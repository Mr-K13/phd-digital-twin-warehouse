#!/usr/bin/env python3
"""Copy PDFs from tagged Zotero papers into a local folder.

By default, this script retrieves top-level papers tagged ``Rank:Q3`` from
the local Zotero personal library and copies their PDF child attachments to
``source/References`` beside this script.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import url2pathname

from upload_ranked_pdfs_to_zotero import (
    DEFAULT_ZOTERO_URL,
    ZoteroError,
    ZoteroLocalClient,
    item_data,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "source" / "References"
REPORT_FIELDS = [
    "Paper title",
    "Paper key",
    "Attachment key",
    "Zotero filename",
    "Source path",
    "Destination path",
    "Action",
    "Message",
]


def is_pdf_attachment(data: dict[str, Any]) -> bool:
    content_type = str(data.get("contentType", "")).casefold()
    filename = str(data.get("filename", ""))
    return (
        data.get("itemType") == "attachment"
        and (content_type == "application/pdf" or filename.casefold().endswith(".pdf"))
    )


def file_url_to_path(file_url: str) -> Path:
    parsed = urlparse(file_url.strip())
    if parsed.scheme != "file":
        raise ValueError(f"Zotero returned a non-file URL: {file_url}")
    path_text = unquote(parsed.path)
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        path_text = f"//{parsed.netloc}{path_text}"
    return Path(url2pathname(path_text))


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def destination_for(
    source: Path,
    output_dir: Path,
    attachment_key: str,
    preferred_filename: str,
) -> tuple[Path, str]:
    """Choose an idempotent, non-destructive destination path."""
    safe_name = Path(preferred_filename).name if preferred_filename else source.name
    if not safe_name.casefold().endswith(".pdf"):
        safe_name = f"{safe_name}.pdf"
    destination = output_dir / safe_name
    if not destination.exists():
        return destination, ""
    if destination.stat().st_size == source.stat().st_size:
        if file_digest(destination) == file_digest(source):
            return destination, "identical file already exists"
    suffix = attachment_key or file_digest(source)[:8]
    alternative = output_dir / f"{destination.stem}_{suffix}{destination.suffix}"
    return alternative, "filename collision; attachment key appended"


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
        description="Copy PDF attachments from Zotero papers matching a tag."
    )
    parser.add_argument("--tag", default="Rank:Q3")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--zotero-url", default=DEFAULT_ZOTERO_URL)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=SCRIPT_DIR / "zotero_pdf_download_report.csv",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.expanduser().resolve()
    client = ZoteroLocalClient(args.zotero_url, timeout=args.timeout)
    try:
        client.connect()
        query = urlencode({"tag": args.tag, "format": "json"})
        papers = client.get_json(f"/users/0/items/top?{query}")
    except ZoteroError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    failures = 0
    copied = 0
    for paper_number, paper in enumerate(papers, 1):
        paper_data = item_data(paper)
        paper_key = str(paper_data.get("key", ""))
        paper_title = str(paper_data.get("title", ""))
        try:
            children = client.get_json(f"/users/0/items/{paper_key}/children")
        except Exception as exc:
            failures += 1
            print(
                f"[{paper_number}/{len(papers)}] {paper_title}: failed — {exc}",
                file=sys.stderr,
            )
            continue

        attachments = [child for child in children if is_pdf_attachment(item_data(child))]
        if not attachments:
            print(
                f"[{paper_number}/{len(papers)}] {paper_title}: no PDF attachment",
                file=sys.stderr,
            )
        for attachment in attachments:
            data = item_data(attachment)
            attachment_key = str(data.get("key", ""))
            zotero_filename = str(data.get("filename", ""))
            source: Optional[Path] = None
            destination: Optional[Path] = None
            action = "Failed"
            message = ""
            try:
                response = client._request(
                    "GET",
                    f"/users/0/items/{attachment_key}/file/view/url",
                )
                source = file_url_to_path(response.body.decode("utf-8"))
                if not source.is_file():
                    raise FileNotFoundError(f"Zotero file is not available locally: {source}")
                destination, collision_note = destination_for(
                    source,
                    output_dir,
                    attachment_key,
                    zotero_filename,
                )
                if collision_note == "identical file already exists":
                    action = "Already exists"
                    message = collision_note
                elif args.dry_run:
                    action = "Would copy"
                    message = collision_note or "No files changed"
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                    copied += 1
                    action = "Copied"
                    message = collision_note
            except Exception as exc:
                failures += 1
                message = str(exc)

            print(
                f"[{paper_number}/{len(papers)}] {paper_title}: {action}"
                + (f" — {message}" if message else ""),
                file=sys.stderr,
            )
            rows.append(
                {
                    "Paper title": paper_title,
                    "Paper key": paper_key,
                    "Attachment key": attachment_key,
                    "Zotero filename": zotero_filename,
                    "Source path": str(source) if source is not None else "",
                    "Destination path": (
                        str(destination) if destination is not None else ""
                    ),
                    "Action": action,
                    "Message": message,
                }
            )

    report = args.report.expanduser().resolve()
    write_report(rows, report)
    print(
        f"Matched {len(papers)} papers tagged {args.tag}; copied {copied} PDFs "
        f"to {output_dir}; report: {report}",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
