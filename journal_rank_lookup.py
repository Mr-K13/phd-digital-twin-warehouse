#!/usr/bin/env python3
"""Resolve paper DOI links and report their journals' SCImago quartiles.

The script uses Crossref for article metadata and a yearly SCImago Journal
Rank (SJR) CSV export for journal quartiles.  Quartiles belong to journals,
not to individual papers.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen


CROSSREF_BASE = "https://api.crossref.org"
DOAJ_BASE = "https://doaj.org/api"
SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = SCRIPT_DIR / ".cache" / "journal-rank-lookup"
# SCImago editions are labelled by the year of their underlying metrics.
# The latest edition released during 2026 contains 2025 metrics.
SCIMAGO_RELEASE_YEAR = 2026
LATEST_SCIMAGO_YEAR = 2025
SCIMAGO_OFFICIAL_URL = (
    "https://www.scimagojr.com/journalrank.php?year={year}&type=all&out=xls"
)
SCIMAGO_MIRRORS = (
    "https://raw.githubusercontent.com/"
    "bastianaravenaulagos/DATOS_SCIMAGO_REVISTAS/main/"
    "scimagojr%20{year}.csv",
    "https://raw.githubusercontent.com/flonat/flonat-research/main/"
    ".context/resources/venue-rankings/scimagojr-{year}.csv",
    "https://raw.githubusercontent.com/LukasRoeseler/openjournals/main/"
    "scimagojr_{year}.csv",
)

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
PII_RE = re.compile(r"\bS[0-9X]{16,}\b", re.IGNORECASE)
ISSN_RE = re.compile(r"\b\d{4}-?\d{3}[\dX]\b", re.IGNORECASE)


class LookupFailure(RuntimeError):
    """A recoverable problem resolving metadata or a journal rank."""


@dataclass
class PaperMetadata:
    doi: str
    title: str
    journal: str
    issns: list[str]
    publication_year: Optional[int]


@dataclass
class Result:
    input: str
    doi: str = ""
    paper_title: str = ""
    journal: str = ""
    issns: str = ""
    publication_year: Optional[int] = None
    ranking_year: Optional[int] = None
    quartile: str = ""
    sjr: str = ""
    global_rank: str = ""
    categories: str = ""
    match_method: str = ""
    status: str = "error"
    message: str = ""


def normalize_doi(value: str) -> Optional[str]:
    """Return a canonical DOI from a raw DOI or DOI URL, if present."""
    text = unquote(value.strip())
    text = re.sub(r"^doi\s*:\s*", "", text, flags=re.IGNORECASE)
    match = DOI_RE.search(text)
    if not match:
        return None
    # Common punctuation from prose is not part of a DOI. Parentheses are
    # valid DOI characters, so remove only clearly trailing delimiters.
    return match.group(0).rstrip(".,;:!?\"'").lower()


def normalize_issn(value: str) -> str:
    return re.sub(r"[^0-9X]", "", value.upper())


def extract_issns(value: str) -> set[str]:
    return {normalize_issn(x) for x in ISSN_RE.findall(value or "")}


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _request_bytes(
    url: str,
    *,
    timeout: int,
    user_agent: str,
    accept: str = "application/json",
) -> bytes:
    req = Request(url, headers={"User-Agent": user_agent, "Accept": accept})
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        raise LookupFailure(f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise LookupFailure(f"Network error for {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LookupFailure(f"Timed out requesting {url}") from exc


def _crossref_url(path: str, mailto: Optional[str]) -> str:
    separator = "&" if "?" in path else "?"
    suffix = f"{separator}mailto={quote(mailto)}" if mailto else ""
    return f"{CROSSREF_BASE}{path}{suffix}"


def _publication_year(item: dict) -> Optional[int]:
    for key in ("published-print", "published-online", "published", "issued"):
        parts = item.get(key, {}).get("date-parts", [])
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                pass
    return None


def _metadata_from_crossref_item(item: dict) -> PaperMetadata:
    doi = normalize_doi(str(item.get("DOI", "")))
    if not doi:
        raise LookupFailure("Crossref record does not contain a valid DOI")
    titles = item.get("title") or []
    journals = item.get("container-title") or []
    journal = html.unescape(str(journals[0])).strip() if journals else ""
    if not journal:
        raise LookupFailure(f"Crossref returned no journal title for {doi}")
    issns = sorted(
        {
            normalize_issn(str(value))
            for value in (item.get("ISSN") or [])
            if normalize_issn(str(value))
        }
    )
    return PaperMetadata(
        doi=doi,
        title=html.unescape(str(titles[0])).strip() if titles else "",
        journal=journal,
        issns=issns,
        publication_year=_publication_year(item),
    )


def fetch_crossref_doi(
    doi: str, *, timeout: int, user_agent: str, mailto: Optional[str]
) -> PaperMetadata:
    path = f"/works/{quote(doi, safe='')}"
    try:
        payload = json.loads(
            _request_bytes(
                _crossref_url(path, mailto), timeout=timeout, user_agent=user_agent
            ).decode("utf-8")
        )
        return _metadata_from_crossref_item(payload["message"])
    except LookupFailure as exc:
        # A small number of valid publisher DOIs are absent from Crossref's
        # REST index. Semantic Scholar can still provide title, journal, and
        # year; journal matching then falls back to the exact journal title.
        if "HTTP 404" not in str(exc):
            raise
        try:
            return fetch_doaj_doi(doi, timeout=timeout, user_agent=user_agent)
        except LookupFailure:
            return fetch_semantic_scholar_doi(
                doi, timeout=timeout, user_agent=user_agent
            )


def fetch_doaj_doi(
    doi: str, *, timeout: int, user_agent: str
) -> PaperMetadata:
    url = f"{DOAJ_BASE}/search/articles/doi:{quote(doi, safe='')}"
    payload = json.loads(
        _request_bytes(url, timeout=timeout, user_agent=user_agent).decode("utf-8")
    )
    results = payload.get("results") or []
    if not results:
        raise LookupFailure(f"DOAJ found no article metadata for {doi}")
    bibjson = results[0].get("bibjson") or {}
    identifiers = bibjson.get("identifier") or []
    returned_doi = next(
        (
            item.get("id")
            for item in identifiers
            if str(item.get("type", "")).casefold() == "doi"
        ),
        doi,
    )
    normalized_doi = normalize_doi(str(returned_doi))
    journal_data = bibjson.get("journal") or {}
    journal = html.unescape(str(journal_data.get("title") or "")).strip()
    issns = sorted(
        {
            normalize_issn(str(value))
            for value in (journal_data.get("issns") or [])
            if normalize_issn(str(value))
        }
    )
    if not normalized_doi or not journal:
        raise LookupFailure(f"DOAJ returned incomplete journal metadata for {doi}")
    try:
        publication_year = int(bibjson.get("year")) if bibjson.get("year") else None
    except (TypeError, ValueError):
        publication_year = None
    return PaperMetadata(
        doi=normalized_doi,
        title=html.unescape(str(bibjson.get("title") or "")).strip(),
        journal=journal,
        issns=issns,
        publication_year=publication_year,
    )


def fetch_semantic_scholar_doi(
    doi: str, *, timeout: int, user_agent: str
) -> PaperMetadata:
    fields = "title,venue,year,externalIds,journal"
    url = (
        f"{SEMANTIC_SCHOLAR_BASE}/paper/DOI:{quote(doi, safe='')}"
        f"?fields={quote(fields, safe=',')}"
    )
    payload = json.loads(
        _request_bytes(url, timeout=timeout, user_agent=user_agent).decode("utf-8")
    )
    external_doi = (payload.get("externalIds") or {}).get("DOI") or doi
    normalized_doi = normalize_doi(str(external_doi))
    journal_data = payload.get("journal") or {}
    journal = html.unescape(
        str(journal_data.get("name") or payload.get("venue") or "")
    ).strip()
    if not normalized_doi or not journal:
        raise LookupFailure(
            f"Semantic Scholar returned incomplete journal metadata for {doi}"
        )
    year = payload.get("year")
    try:
        publication_year = int(year) if year else None
    except (TypeError, ValueError):
        publication_year = None
    return PaperMetadata(
        doi=normalized_doi,
        title=html.unescape(str(payload.get("title") or "")).strip(),
        journal=journal,
        issns=[],
        publication_year=publication_year,
    )


def fetch_crossref_query(
    query: str, *, timeout: int, user_agent: str, mailto: Optional[str]
) -> PaperMetadata:
    """Resolve a publisher identifier such as a ScienceDirect PII."""
    path = f"/works?query.bibliographic={quote(query)}&rows=5"
    payload = json.loads(
        _request_bytes(
            _crossref_url(path, mailto), timeout=timeout, user_agent=user_agent
        ).decode("utf-8")
    )
    items = payload.get("message", {}).get("items", [])
    if not items:
        raise LookupFailure(f"Crossref found no record for publisher ID {query}")

    needle = query.casefold()
    for item in items:
        candidates = [str(x).casefold() for x in item.get("alternative-id", [])]
        candidates.append(str(item.get("URL", "")).casefold())
        if any(needle in candidate for candidate in candidates):
            return _metadata_from_crossref_item(item)

    # Crossref's first result is usually the correct one for an exact PII, but
    # do not accept a weak result silently.
    first = items[0]
    if float(first.get("score", 0) or 0) >= 50:
        return _metadata_from_crossref_item(first)
    raise LookupFailure(f"Crossref could not confidently resolve publisher ID {query}")


def _query_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 1
    }


def fetch_crossref_title_query(
    title_fragment: str,
    *,
    timeout: int,
    user_agent: str,
    mailto: Optional[str],
) -> PaperMetadata:
    """Resolve a blocked publisher URL using a human-readable title slug."""
    path = f"/works?query.title={quote(title_fragment)}&rows=5"
    payload = json.loads(
        _request_bytes(
            _crossref_url(path, mailto), timeout=timeout, user_agent=user_agent
        ).decode("utf-8")
    )
    query_tokens = _query_tokens(title_fragment)
    if len(query_tokens) < 4:
        raise LookupFailure("Article URL does not contain enough title text for a safe search")
    for item in payload.get("message", {}).get("items", []):
        titles = item.get("title") or []
        if not titles:
            continue
        candidate_tokens = _query_tokens(html.unescape(str(titles[0])))
        coverage = len(query_tokens & candidate_tokens) / len(query_tokens)
        if coverage >= 0.80:
            return _metadata_from_crossref_item(item)
    raise LookupFailure(
        f"Crossref could not confidently match URL title fragment {title_fragment!r}"
    )


def title_fragment_from_url(url: str) -> Optional[str]:
    segments = [unquote(part) for part in urlparse(url).path.split("/") if part]
    for segment in reversed(segments):
        text = re.sub(r"[-_]+", " ", segment).strip()
        if len(_query_tokens(text)) >= 4 and not text.isdigit():
            return text
    return None


def _doi_from_elsevier_payload(payload: dict) -> str:
    coredata = payload.get("full-text-retrieval-response", {}).get("coredata", {})
    doi = normalize_doi(str(coredata.get("prism:doi", "")))
    if not doi:
        doi = normalize_doi(str(coredata.get("dc:identifier", "")))
    if not doi:
        raise LookupFailure("Elsevier metadata did not contain a DOI")
    return doi


def fetch_elsevier_pii_doi(
    pii: str,
    *,
    timeout: int,
    user_agent: str,
    api_key: Optional[str],
) -> str:
    url = (
        "https://api.elsevier.com/content/article/pii/"
        f"{quote(pii)}?httpAccept=application%2Fjson"
    )
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
    }
    if api_key:
        headers["X-ELS-APIKey"] = api_key
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {401, 403} and not api_key:
            raise LookupFailure(
                "Elsevier requires an API key for this PII. Set "
                "ELSEVIER_API_KEY or pass --elsevier-api-key, or supply the DOI directly"
            ) from exc
        raise LookupFailure(f"Elsevier API returned HTTP {exc.code} for PII {pii}") from exc
    except URLError as exc:
        raise LookupFailure(f"Network error resolving Elsevier PII {pii}: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise LookupFailure(f"Invalid or timed-out Elsevier response for PII {pii}") from exc
    return _doi_from_elsevier_payload(payload)


def scrape_doi_from_page(url: str, *, timeout: int, user_agent: str) -> str:
    content = _request_bytes(
        url, timeout=timeout, user_agent=user_agent, accept="text/html,*/*"
    )
    text = content.decode("utf-8", errors="replace")
    doi = normalize_doi(text)
    if not doi:
        raise LookupFailure(f"No DOI was found on article page {url}")
    return doi


def resolve_paper(
    value: str,
    *,
    timeout: int,
    user_agent: str,
    mailto: Optional[str],
    elsevier_api_key: Optional[str],
) -> PaperMetadata:
    doi = normalize_doi(value)
    if doi:
        return fetch_crossref_doi(
            doi, timeout=timeout, user_agent=user_agent, mailto=mailto
        )

    pii_match = PII_RE.search(unquote(value))
    if pii_match:
        pii = pii_match.group(0)
        try:
            resolved_doi = fetch_elsevier_pii_doi(
                pii,
                timeout=timeout,
                user_agent=user_agent,
                api_key=elsevier_api_key,
            )
            return fetch_crossref_doi(
                resolved_doi,
                timeout=timeout,
                user_agent=user_agent,
                mailto=mailto,
            )
        except LookupFailure as elsevier_error:
            try:
                return fetch_crossref_query(
                    pii,
                    timeout=timeout,
                    user_agent=user_agent,
                    mailto=mailto,
                )
            except LookupFailure:
                raise elsevier_error

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        try:
            doi = scrape_doi_from_page(value, timeout=timeout, user_agent=user_agent)
            return fetch_crossref_doi(
                doi, timeout=timeout, user_agent=user_agent, mailto=mailto
            )
        except LookupFailure as page_error:
            title_fragment = title_fragment_from_url(value)
            if title_fragment:
                try:
                    return fetch_crossref_title_query(
                        title_fragment,
                        timeout=timeout,
                        user_agent=user_agent,
                        mailto=mailto,
                    )
                except LookupFailure:
                    pass
            raise page_error
    raise LookupFailure("Input is neither a DOI, DOI URL, nor resolvable article URL")


def _decode_csv(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise LookupFailure("Could not decode the SCImago CSV")


def _validate_sjr_csv(text: str) -> None:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    required = ("Title", "Issn", "SJR Best Quartile")
    if not all(field.casefold() in first_line.casefold() for field in required):
        raise LookupFailure(
            "Downloaded content is not a SCImago journal ranking CSV "
            f"(header was {first_line[:120]!r})"
        )


def obtain_sjr_file(
    *,
    year: int,
    supplied_file: Optional[Path],
    cache_dir: Path,
    timeout: int,
    user_agent: str,
) -> tuple[Path, str]:
    if supplied_file:
        if not supplied_file.is_file():
            raise LookupFailure(f"SCImago file does not exist: {supplied_file}")
        text = _decode_csv(supplied_file.read_bytes())
        _validate_sjr_csv(text)
        return supplied_file, f"local file: {supplied_file}"

    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"scimagojr_{year}.csv"
    if cached.is_file():
        try:
            _validate_sjr_csv(_decode_csv(cached.read_bytes()))
            return cached, f"cached SCImago export: {cached}"
        except LookupFailure:
            cached.unlink()

    urls = [SCIMAGO_OFFICIAL_URL.format(year=year)] + [
        template.format(year=year) for template in SCIMAGO_MIRRORS
    ]
    errors: list[str] = []
    for url in urls:
        try:
            payload = _request_bytes(
                url,
                timeout=timeout,
                user_agent=user_agent,
                accept="text/csv,application/csv,text/plain,*/*",
            )
            text = _decode_csv(payload)
            _validate_sjr_csv(text)
            cached.write_text(text, encoding="utf-8")
            label = "official SCImago export" if "scimagojr.com" in url else "mirror of SCImago export"
            return cached, f"{label}: {url}"
        except LookupFailure as exc:
            errors.append(str(exc))
    raise LookupFailure(
        "Unable to download a valid SCImago ranking file. Download the CSV "
        "manually from https://www.scimagojr.com/journalrank.php and pass it "
        "with --sjr-file. Attempts: " + " | ".join(errors)
    )


def load_sjr_rows(path: Path) -> list[dict[str, str]]:
    text = _decode_csv(path.read_bytes())
    sample = text[:65536]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";"
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for raw in reader:
        row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
        if row.get("Title"):
            rows.append(row)
    if not rows:
        raise LookupFailure(f"No journal records found in {path}")
    return rows


class SJRIndex:
    def __init__(self, rows: Iterable[dict[str, str]]):
        self.by_issn: dict[str, list[dict[str, str]]] = {}
        self.by_title: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            for issn in extract_issns(row.get("Issn", "")):
                self.by_issn.setdefault(issn, []).append(row)
            title_key = normalize_title(row.get("Title", ""))
            if title_key:
                self.by_title.setdefault(title_key, []).append(row)

    def find(self, metadata: PaperMetadata) -> tuple[Optional[dict[str, str]], str]:
        for issn in metadata.issns:
            matches = self.by_issn.get(normalize_issn(issn), [])
            if matches:
                exact_title = normalize_title(metadata.journal)
                for row in matches:
                    if normalize_title(row.get("Title", "")) == exact_title:
                        return row, "ISSN + journal title"
                return matches[0], "ISSN"
        matches = self.by_title.get(normalize_title(metadata.journal), [])
        if matches:
            return matches[0], "exact normalized journal title"
        return None, ""


def _first_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        if row.get(name):
            return row[name]
    return ""


def rank_input(
    value: str,
    *,
    index: SJRIndex,
    ranking_year: int,
    timeout: int,
    user_agent: str,
    mailto: Optional[str],
    elsevier_api_key: Optional[str],
) -> Result:
    result = Result(input=value, ranking_year=ranking_year)
    try:
        metadata = resolve_paper(
            value,
            timeout=timeout,
            user_agent=user_agent,
            mailto=mailto,
            elsevier_api_key=elsevier_api_key,
        )
        result.doi = metadata.doi
        result.paper_title = metadata.title
        result.journal = metadata.journal
        result.issns = ", ".join(metadata.issns)
        result.publication_year = metadata.publication_year
        row, method = index.find(metadata)
        if row is None:
            result.status = "not_found"
            result.message = (
                "Journal was resolved but was not found in the selected "
                "SCImago ranking export"
            )
            return result
        quartile = _first_value(
            row, "SJR Best Quartile", "Best Quartile", "Quartile"
        ).upper()
        result.quartile = "" if quartile in {"", "-", "N/A", "NA", "Q0"} else quartile
        result.sjr = _first_value(row, "SJR")
        result.global_rank = _first_value(row, "Rank")
        result.categories = _first_value(row, "Categories")
        result.match_method = method
        result.status = "ok" if result.quartile else "unranked"
        result.message = (
            "SCImago best quartile for the journal; the paper itself has no quartile"
            if result.quartile
            else "Journal is present in SCImago, but no quartile is assigned for this year"
        )
    except (LookupFailure, KeyError, ValueError, json.JSONDecodeError) as exc:
        result.message = str(exc)
    return result


def read_inputs(values: list[str], input_file: Optional[Path]) -> list[str]:
    output = [value.strip() for value in values if value.strip()]
    if not input_file:
        return output
    if not input_file.is_file():
        raise LookupFailure(f"Input file does not exist: {input_file}")
    text = input_file.read_text(encoding="utf-8-sig")
    if input_file.suffix.casefold() == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return output
        normalized = {name.casefold().strip(): name for name in reader.fieldnames}
        column = next(
            (normalized[name] for name in ("doi", "url", "link") if name in normalized),
            reader.fieldnames[0],
        )
        output.extend(
            str(row.get(column, "")).strip()
            for row in reader
            if str(row.get(column, "")).strip()
        )
    else:
        output.extend(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return output


def print_table(results: list[Result]) -> None:
    headers = ["Quartile", "Journal", "Year", "DOI", "Status"]
    rows = [
        [
            item.quartile or "—",
            item.journal or "—",
            str(item.ranking_year or "—"),
            item.doi or item.input,
            item.status,
        ]
        for item in results
    ]
    widths = [
        min(max(len(str(row[i])) for row in [headers, *rows]), limit)
        for i, limit in enumerate((8, 46, 6, 42, 10))
    ]

    def render(row: list[str]) -> str:
        cells = []
        for value, width in zip(row, widths):
            text = str(value)
            if len(text) > width:
                text = text[: max(1, width - 1)] + "…"
            cells.append(text.ljust(width))
        return " | ".join(cells)

    print(render(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(render(row))


def write_results(results: list[Result], output_format: str, output: Optional[Path]) -> None:
    if output_format == "table" and output is None:
        print_table(results)
        return
    stream: io.TextIOBase
    close_stream = False
    if output:
        stream = output.open("w", encoding="utf-8", newline="")
        close_stream = True
    else:
        stream = sys.stdout
    try:
        if output_format == "json":
            json.dump([asdict(item) for item in results], stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        elif output_format == "csv":
            fields = list(Result.__dataclass_fields__)
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(asdict(item) for item in results)
        else:
            # A table redirected to a file is deliberately plain text.
            old_stdout = sys.stdout
            sys.stdout = stream
            try:
                print_table(results)
            finally:
                sys.stdout = old_stdout
    finally:
        if close_stream:
            stream.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve DOI links and report each paper journal's SCImago "
            "best quartile (Q1-Q4)."
        )
    )
    parser.add_argument("identifiers", nargs="*", help="DOIs, DOI URLs, or article URLs")
    parser.add_argument("--input", type=Path, help="TXT file or CSV with a doi/url/link column")
    parser.add_argument(
        "--sjr-file", type=Path, help="Previously downloaded SCImago journal-ranking CSV"
    )
    parser.add_argument(
        "--rank-year",
        type=int,
        default=LATEST_SCIMAGO_YEAR,
        help=(
            "SCImago metric year "
            f"(default: {LATEST_SCIMAGO_YEAR}, latest edition released in "
            f"{SCIMAGO_RELEASE_YEAR})"
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=(
            "Directory for downloaded SCImago CSV files "
            f"(default: {DEFAULT_CACHE_DIR})"
        ),
    )
    parser.add_argument("--format", choices=("table", "csv", "json"), default="table")
    parser.add_argument("--output", type=Path, help="Write results to this file")
    parser.add_argument("--mailto", help="Email for the Crossref polite API pool")
    parser.add_argument(
        "--elsevier-api-key",
        default=os.environ.get("ELSEVIER_API_KEY"),
        help=(
            "Optional Elsevier API key for ScienceDirect PII URLs "
            "(default: ELSEVIER_API_KEY environment variable)"
        ),
    )
    parser.add_argument("--timeout", type=int, default=30, help="Network timeout in seconds")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay between Crossref requests in seconds (default: 0.1)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    user_agent = "journal-rank-lookup/1.0 (+https://www.crossref.org/)"
    if args.mailto:
        user_agent = f"journal-rank-lookup/1.0 (mailto:{args.mailto})"
    try:
        inputs = read_inputs(args.identifiers, args.input)
        if not inputs:
            raise LookupFailure("No DOI links were supplied")
        sjr_path, source = obtain_sjr_file(
            year=args.rank_year,
            supplied_file=args.sjr_file,
            cache_dir=args.cache_dir,
            timeout=args.timeout,
            user_agent=user_agent,
        )
        index = SJRIndex(load_sjr_rows(sjr_path))
    except LookupFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    results = []
    for position, value in enumerate(inputs):
        if position and args.delay:
            time.sleep(args.delay)
        results.append(
            rank_input(
                value,
                index=index,
                ranking_year=args.rank_year,
                timeout=args.timeout,
                user_agent=user_agent,
                mailto=args.mailto,
                elsevier_api_key=args.elsevier_api_key,
            )
        )
    write_results(results, args.format, args.output)
    print(f"Ranking data: {source}", file=sys.stderr)
    return 0 if all(item.status == "ok" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
