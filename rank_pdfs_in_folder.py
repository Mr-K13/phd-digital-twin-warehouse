#!/usr/bin/env python3
"""Extract DOIs from PDFs and export their journals' SCImago quartiles.

This script imports ``journal_rank_lookup.py`` so DOI metadata, journal
matching, and SCImago lookup use the same implementation as the standalone
DOI-link command.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - exercised only without dependency
    raise SystemExit(
        "pypdf is required. Install it with: python3 -m pip install pypdf"
    ) from exc

import journal_rank_lookup as jrl


DEFAULT_FOLDER = Path(
    "/Users/congdinh/Documents/PhD in Digital Twin/source/References"
)
OUTPUT_FIELDS = [
    "Paper title",
    "DOI",
    "Journal",
    "Publication year",
    "SCImago ranking year",
    "Journal quartile",
]


@dataclass
class DoiCandidate:
    doi: str
    evidence_score: int
    source: str


def clean_doi_candidate(value: str) -> Optional[str]:
    doi = jrl.normalize_doi(value)
    if not doi:
        return None
    lowered = doi.casefold()
    if "(issn)" in lowered or lowered.startswith("10.5555/"):
        return None
    return doi


def find_dois(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in jrl.DOI_RE.finditer(text or ""):
        doi = clean_doi_candidate(match.group(0))
        if doi and doi not in seen:
            seen.add(doi)
            found.append(doi)
    return found


def readable_pdf_title(metadata: object) -> str:
    if not metadata:
        return ""
    try:
        title = str(metadata.get("/Title", "") or "").strip()
    except AttributeError:
        return ""
    if not title or title.casefold() in {"untitled", "unknown", "none"}:
        return ""
    if title.startswith(("http://", "https://", "/Users/", "C:\\")):
        return ""
    return title


def title_from_filename(path: Path) -> str:
    title = path.stem
    title = re.sub(r"^\d{1,3}[_\s-]+", "", title)
    title = re.sub(r"^\d{4}[_\s-]+", "", title)
    title = re.sub(r"[_-]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _metadata_text(metadata: object) -> str:
    if not metadata:
        return ""
    try:
        return "\n".join(str(value) for value in dict(metadata).values())
    except (TypeError, ValueError):
        return str(metadata)


def collect_opening_candidates(
    reader: PdfReader, *, opening_pages: int
) -> tuple[list[DoiCandidate], str, str]:
    scores: dict[str, int] = defaultdict(int)
    sources: dict[str, list[str]] = defaultdict(list)
    metadata = reader.metadata or {}
    pdf_title = readable_pdf_title(metadata)

    for doi in find_dois(_metadata_text(metadata)):
        scores[doi] = max(scores[doi], 100)
        sources[doi].append("PDF metadata")

    page_texts: list[str] = []
    for index in range(min(opening_pages, len(reader.pages))):
        try:
            text = reader.pages[index].extract_text() or ""
        except Exception:
            text = ""
        page_texts.append(text)
        base_score = 85 if index == 0 else max(55, 75 - index * 8)
        for doi in find_dois(text):
            scores[doi] = max(scores[doi], base_score)
            sources[doi].append(f"page {index + 1}")

    candidates = [
        DoiCandidate(doi, score, ", ".join(dict.fromkeys(sources[doi])))
        for doi, score in scores.items()
    ]
    candidates.sort(key=lambda item: (-item.evidence_score, item.doi))
    return candidates, pdf_title, "\n".join(page_texts)


def collect_full_document_candidates(
    reader: PdfReader, existing: set[str]
) -> list[DoiCandidate]:
    output: list[DoiCandidate] = []
    seen = set(existing)
    for index, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        for doi in find_dois(text):
            if doi not in seen:
                seen.add(doi)
                output.append(DoiCandidate(doi, 15, f"full-text page {index + 1}"))
    return output


def token_coverage(needle: str, haystack: str) -> float:
    needle_tokens = jrl._query_tokens(needle)
    if not needle_tokens:
        return 0.0
    haystack_tokens = jrl._query_tokens(haystack)
    return len(needle_tokens & haystack_tokens) / len(needle_tokens)


def choose_validated_candidate(
    candidates: list[DoiCandidate],
    *,
    title_hint: str,
    opening_text: str,
    timeout: int,
    user_agent: str,
    mailto: Optional[str],
    limit: int = 12,
) -> tuple[Optional[str], Optional[jrl.PaperMetadata], str]:
    best: tuple[float, str, jrl.PaperMetadata, str] | None = None
    errors: list[str] = []
    for candidate in candidates[:limit]:
        try:
            metadata = jrl.fetch_crossref_doi(
                candidate.doi,
                timeout=timeout,
                user_agent=user_agent,
                mailto=mailto,
            )
        except Exception as exc:
            errors.append(f"{candidate.doi}: {exc}")
            continue
        title_match = max(
            token_coverage(metadata.title, opening_text),
            token_coverage(title_hint, metadata.title),
        )
        total = candidate.evidence_score + 100 * title_match
        current = (total, candidate.doi, metadata, candidate.source)
        if best is None or current[0] > best[0]:
            best = current

    if best is None:
        return None, None, "; ".join(errors[-3:])
    return best[1], best[2], best[3]


def search_doi_by_title(
    title_hint: str,
    *,
    timeout: int,
    user_agent: str,
    mailto: Optional[str],
) -> tuple[Optional[str], Optional[jrl.PaperMetadata]]:
    if len(jrl._query_tokens(title_hint)) < 4:
        return None, None
    try:
        metadata = jrl.fetch_crossref_title_query(
            title_hint,
            timeout=timeout,
            user_agent=user_agent,
            mailto=mailto,
        )
    except Exception:
        return None, None
    return metadata.doi, metadata


def extract_pdf_doi(
    path: Path,
    *,
    opening_pages: int,
    timeout: int,
    user_agent: str,
    mailto: Optional[str],
) -> tuple[Optional[str], str, str]:
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        return None, title_from_filename(path), f"cannot read PDF: {exc}"

    opening, embedded_title, opening_text = collect_opening_candidates(
        reader, opening_pages=opening_pages
    )
    filename_title = title_from_filename(path)
    title_hint = embedded_title or filename_title

    doi, metadata, evidence = choose_validated_candidate(
        opening,
        title_hint=title_hint,
        opening_text=opening_text,
        timeout=timeout,
        user_agent=user_agent,
        mailto=mailto,
    )
    if doi and metadata:
        return doi, metadata.title or title_hint, evidence

    # For PDFs without a DOI on the opening pages, an exact title search is
    # safer than selecting the first DOI from a bibliography.
    doi, metadata = search_doi_by_title(
        title_hint,
        timeout=timeout,
        user_agent=user_agent,
        mailto=mailto,
    )
    if doi and metadata:
        return doi, metadata.title or title_hint, "Crossref title search"

    full_document = collect_full_document_candidates(
        reader, {item.doi for item in opening}
    )
    doi, metadata, evidence = choose_validated_candidate(
        full_document,
        title_hint=title_hint,
        opening_text=opening_text,
        timeout=timeout,
        user_agent=user_agent,
        mailto=mailto,
    )
    if doi and metadata:
        # Require title evidence for a DOI found only in the bibliography.
        similarity = max(
            token_coverage(metadata.title, opening_text),
            token_coverage(title_hint, metadata.title),
        )
        if similarity >= 0.55:
            return doi, metadata.title or title_hint, evidence

    return None, title_hint, "no confidently matched DOI"


def write_csv_atomic(rows: list[dict[str, object]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract DOI metadata and SCImago journal quartiles from PDFs."
    )
    parser.add_argument(
        "--folder",
        type=Path,
        default=DEFAULT_FOLDER,
        help=f"Folder containing PDF files (default: {DEFAULT_FOLDER})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV (default: <folder>/journal_quartiles.csv)",
    )
    parser.add_argument(
        "--rank-year",
        type=int,
        default=jrl.LATEST_SCIMAGO_YEAR,
        help=(
            "SCImago metric year "
            f"(default: {jrl.LATEST_SCIMAGO_YEAR}, latest edition released in "
            f"{jrl.SCIMAGO_RELEASE_YEAR})"
        ),
    )
    parser.add_argument("--sjr-file", type=Path, help="Local SCImago CSV export")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=jrl.DEFAULT_CACHE_DIR,
        help=f"SCImago cache directory (default: {jrl.DEFAULT_CACHE_DIR})",
    )
    parser.add_argument("--mailto", help="Email for Crossref's polite API pool")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--opening-pages", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--recursive", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    folder = args.folder.expanduser().resolve()
    output = (args.output or folder / "journal_quartiles.csv").expanduser().resolve()
    if not folder.is_dir():
        print(f"error: PDF folder does not exist: {folder}", file=sys.stderr)
        return 2

    pattern = "**/*.pdf" if args.recursive else "*.pdf"
    pdfs = sorted(folder.glob(pattern), key=lambda item: item.name.casefold())
    if not pdfs:
        print(f"error: no PDF files found in {folder}", file=sys.stderr)
        return 2

    user_agent = "pdf-journal-rank/1.0 (+https://www.crossref.org/)"
    if args.mailto:
        user_agent = f"pdf-journal-rank/1.0 (mailto:{args.mailto})"
    try:
        sjr_path, sjr_source = jrl.obtain_sjr_file(
            year=args.rank_year,
            supplied_file=args.sjr_file,
            cache_dir=args.cache_dir,
            timeout=args.timeout,
            user_agent=user_agent,
        )
        index = jrl.SJRIndex(jrl.load_sjr_rows(sjr_path))
    except jrl.LookupFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rows: list[dict[str, object]] = []
    unresolved = 0
    for number, pdf in enumerate(pdfs, 1):
        if number > 1 and args.delay:
            time.sleep(args.delay)
        doi, fallback_title, evidence = extract_pdf_doi(
            pdf,
            opening_pages=max(1, args.opening_pages),
            timeout=args.timeout,
            user_agent=user_agent,
            mailto=args.mailto,
        )
        result: Optional[jrl.Result] = None
        if doi:
            result = jrl.rank_input(
                doi,
                index=index,
                ranking_year=args.rank_year,
                timeout=args.timeout,
                user_agent=user_agent,
                mailto=args.mailto,
                elsevier_api_key=None,
            )

        if result and result.doi:
            row = {
                "Paper title": result.paper_title or fallback_title,
                "DOI": result.doi,
                "Journal": result.journal,
                "Publication year": result.publication_year or "",
                "SCImago ranking year": args.rank_year,
                "Journal quartile": result.quartile or "Unranked",
            }
            label = result.quartile or "Unranked"
            print(
                f"[{number}/{len(pdfs)}] {pdf.name}: {label} "
                f"({result.journal}; {evidence})",
                file=sys.stderr,
            )
        else:
            unresolved += 1
            row = {
                "Paper title": fallback_title,
                "DOI": doi or "",
                "Journal": result.journal if result else "",
                "Publication year": result.publication_year if result else "",
                "SCImago ranking year": args.rank_year,
                "Journal quartile": "Unresolved",
            }
            reason = result.message if result else evidence
            print(
                f"[{number}/{len(pdfs)}] {pdf.name}: unresolved ({reason})",
                file=sys.stderr,
            )
        rows.append(row)

    write_csv_atomic(rows, output)
    print(f"Wrote {len(rows)} rows to {output}", file=sys.stderr)
    print(f"Ranking data: {sjr_source}", file=sys.stderr)
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
