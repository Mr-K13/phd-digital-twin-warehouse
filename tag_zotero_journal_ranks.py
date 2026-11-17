#!/usr/bin/env python3
"""Add SCImago quartile tags to unranked papers in a local Zotero library."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import journal_rank_lookup as jrl
from upload_ranked_pdfs_to_zotero import (
    DEFAULT_ZOTERO_URL,
    ZoteroError,
    ZoteroLocalClient,
    item_data,
    merge_rank_tag,
    merge_tags,
)


REPORT_FIELDS = [
    "Zotero item key",
    "Paper title",
    "DOI",
    "Journal",
    "SCImago ranking year",
    "Journal quartile",
    "Matched additional tags",
    "Action",
    "Message",
]
NON_PAPER_TYPES = {"attachment", "note", "annotation"}


def has_rank_tag(tags: list[dict[str, Any]]) -> bool:
    return any(
        str(tag.get("tag", "")).strip().casefold().startswith("rank:")
        for tag in (tags or [])
    )


def is_paper_item(data: dict[str, Any]) -> bool:
    return str(data.get("itemType", "")) not in NON_PAPER_TYPES


def rank_tag_value(result: jrl.Result) -> str:
    """Map a lookup result to Q1-Q4, NA, or no tag for transient failures."""
    if re.fullmatch(r"Q[1-4]", result.quartile or "", flags=re.IGNORECASE):
        return result.quartile.upper()
    if result.status in {"not_found", "unranked"}:
        return "NA"
    return ""


def matching_additional_tags(
    data: dict[str, Any], requested: list[str]
) -> list[str]:
    """Return requested tags found literally in Title or Abstract, ignoring case."""
    searchable = (
        f"{data.get('title', '')}\n{data.get('abstractNote', '')}".casefold()
    )
    matches: list[str] = []
    seen: set[str] = set()
    for value in requested:
        tag = value.strip()
        folded = tag.casefold()
        if tag and folded not in seen and folded in searchable:
            matches.append(tag)
            seen.add(folded)
    return matches


def write_report(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Add missing SCImago Rank: tags to Zotero papers and optionally "
            "add supplied tags when they occur in Title or Abstract."
        )
    )
    parser.add_argument("--rank-year", type=int, default=jrl.LATEST_SCIMAGO_YEAR)
    parser.add_argument("--sjr-file", type=Path)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=jrl.DEFAULT_CACHE_DIR,
        help=f"SCImago cache directory (default: {jrl.DEFAULT_CACHE_DIR})",
    )
    parser.add_argument("--zotero-url", default=DEFAULT_ZOTERO_URL)
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ZOTERO_LOCAL_API_KEY"),
        help="Remembered local key (default: ZOTERO_LOCAL_API_KEY); normally omit",
    )
    parser.add_argument("--mailto", help="Email for Crossref's polite API pool")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help=(
            "Tag to add when it occurs in the paper Title or Abstract; "
            "repeat to supply multiple tags"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--recheck-ranks",
        action="store_true",
        help="Recheck and replace existing Rank: tags; default is to skip them",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(__file__).resolve().parent / "zotero_rank_tag_report.csv",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    user_agent = "zotero-journal-rank-tagger/1.0 (+https://www.zotero.org/)"
    if args.mailto:
        user_agent = f"zotero-journal-rank-tagger/1.0 (mailto:{args.mailto})"
    try:
        sjr_path, sjr_source = jrl.obtain_sjr_file(
            year=args.rank_year,
            supplied_file=args.sjr_file,
            cache_dir=args.cache_dir,
            timeout=args.timeout,
            user_agent=user_agent,
        )
        rank_index = jrl.SJRIndex(jrl.load_sjr_rows(sjr_path))
    except jrl.LookupFailure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    client = ZoteroLocalClient(
        args.zotero_url, timeout=args.timeout, api_key=args.api_key
    )
    try:
        client.connect()
        items = client.get_json("/users/0/items/top?format=json")
    except ZoteroError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    papers = [item for item in items if is_paper_item(item_data(item))]
    rows: list[dict[str, Any]] = []
    failures = 0
    for number, item in enumerate(papers, 1):
        if number > 1 and args.delay:
            time.sleep(args.delay)
        data = item_data(item)
        key = str(data.get("key", ""))
        title = str(data.get("title", ""))
        raw_doi = str(data.get("DOI", "")) or str(data.get("url", ""))
        doi = jrl.normalize_doi(raw_doi) or ""
        journal = str(data.get("publicationTitle", ""))
        quartile = ""
        original_tags = data.get("tags", [])
        matched_tags = matching_additional_tags(data, args.tag)
        new_tags = merge_tags(original_tags, matched_tags)
        notes: list[str] = []
        rank_missing = not has_rank_tag(original_tags)
        rank_needed = rank_missing or args.recheck_ranks
        if rank_needed:
            if not doi:
                notes.append("No DOI available for journal-rank lookup")
            else:
                try:
                    result = jrl.rank_input(
                        doi,
                        index=rank_index,
                        ranking_year=args.rank_year,
                        timeout=args.timeout,
                        user_agent=user_agent,
                        mailto=args.mailto,
                        elsevier_api_key=None,
                    )
                    journal = result.journal or journal
                    rank_value = rank_tag_value(result)
                    quartile = rank_value
                    if rank_value:
                        new_tags = merge_rank_tag(new_tags, rank_value)
                        if rank_value == "NA":
                            notes.append(result.message)
                    else:
                        notes.append(
                            result.message or "Journal-rank lookup did not resolve"
                        )
                except Exception as exc:
                    failures += 1
                    notes.append(f"Journal-rank lookup failed: {exc}")

        old_names = {
            str(tag.get("tag", "")).strip().casefold() for tag in original_tags
        }
        added_names = [
            str(tag.get("tag", "")).strip()
            for tag in new_tags
            if str(tag.get("tag", "")).strip().casefold() not in old_names
        ]
        action = "No changes"
        if new_tags != original_tags:
            try:
                if args.dry_run:
                    action = "Would add " + ", ".join(added_names)
                    notes.append("No Zotero changes made")
                else:
                    client.patch_item_fields(item, {"tags": new_tags})
                    action = "Added " + ", ".join(added_names)
            except Exception as exc:
                failures += 1
                action = "Failed"
                notes.append(str(exc))
        elif rank_needed and notes:
            action = "Skipped"
        message = "; ".join(notes)

        print(
            f"[{number}/{len(papers)}] {title or key}: {action}"
            + (f" — {message}" if message else ""),
            file=sys.stderr,
        )
        rows.append(
            {
                "Zotero item key": key,
                "Paper title": title,
                "DOI": doi,
                "Journal": journal,
                "SCImago ranking year": args.rank_year,
                "Journal quartile": quartile,
                "Matched additional tags": ", ".join(matched_tags),
                "Action": action,
                "Message": message,
            }
        )

    report = args.report.expanduser().resolve()
    write_report(rows, report)
    print(
        f"Processed {len(papers)} papers; report: {report}",
        file=sys.stderr,
    )
    print(f"Ranking data: {sjr_source}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
