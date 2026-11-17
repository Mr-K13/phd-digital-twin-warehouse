#!/usr/bin/env python3
"""Rank PDFs with SCImago and import them through Zotero's Local API.

For each PDF, the script finds a DOI, resolves its journal metadata, obtains
the journal's SCImago quartile via ``journal_rank_lookup.py``, creates or
updates a Zotero journal-article item, adds the quartile as a
``Rank:Q1``-style tag, and uploads the PDF as a stored child attachment. The
Extra field is left unchanged.

Zotero 10 or later must be running with local API access enabled in
Settings -> Advanced -> Allow other applications on this computer to
communicate with Zotero.

python3 upload_ranked_pdfs_to_zotero.py --folder "/Users/congdinh/Documents/PhD in Digital Twin/source/References"

Preview without modifying Zotero:
python3 upload_ranked_pdfs_to_zotero.py \
  --folder "/Users/congdinh/Documents/PhD in Digital Twin/source/References" \
  --dry-run

Include PDFs in subfolders:
python3 upload_ranked_pdfs_to_zotero.py \
  --folder "/Users/congdinh/Documents/PhD in Digital Twin/source/References" \
  --recursive

"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

import journal_rank_lookup as jrl
import rank_pdfs_in_folder as rpf


DEFAULT_ZOTERO_URL = "http://localhost:23119/api"
APP_NAME = "SCImago PDF Importer"
REPORT_FIELDS = [
    "PDF",
    "Paper title",
    "DOI",
    "Journal",
    "SCImago ranking year",
    "Journal quartile",
    "Zotero item key",
    "Action",
    "Message",
]


class ZoteroError(RuntimeError):
    """A local Zotero API request failed."""


@dataclass
class ZoteroResponse:
    status: int
    headers: Any
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


class ZoteroLocalClient:
    """Small Zotero Local API v3 client with runtime write authorization."""

    def __init__(
        self,
        base_url: str = DEFAULT_ZOTERO_URL,
        *,
        timeout: int = 30,
        api_key: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key
        self.server_id = ""
        self.api_version = ""

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        write: bool = False,
        retry_authorization: bool = True,
    ) -> ZoteroResponse:
        request_headers = {
            "Accept": "application/json",
            "Zotero-API-Version": "3",
            **(headers or {}),
        }
        if write:
            if not self.server_id:
                raise ZoteroError("Connect to Zotero before making a write request")
            if not self.api_key:
                self.authorize()
            request_headers["Zotero-Server-ID"] = self.server_id
            request_headers["Zotero-API-Key"] = self.api_key or ""

        request = Request(
            self._url(path), data=body, headers=request_headers, method=method
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return ZoteroResponse(
                    response.status, response.headers, response.read()
                )
        except HTTPError as exc:
            error_body = exc.read()
            if write and exc.code == 401 and retry_authorization:
                self.api_key = None
                self.authorize()
                return self._request(
                    method,
                    path,
                    body=body,
                    headers=headers,
                    write=True,
                    retry_authorization=False,
                )
            detail = error_body.decode("utf-8", errors="replace").strip()
            raise ZoteroError(
                f"Zotero returned HTTP {exc.code} for {method} {path}"
                + (f": {detail}" if detail else "")
            ) from exc
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ZoteroError(f"Cannot reach Zotero at {self.base_url}: {reason}") from exc

    def connect(self) -> None:
        response = self._request("GET", "/")
        self.server_id = response.headers.get("Zotero-Server-ID", "")
        self.api_version = response.headers.get("Zotero-API-Version", "")
        if self.api_version != "3":
            raise ZoteroError(
                f"Zotero Local API version 3 is required; found {self.api_version or 'unknown'}"
            )
        if not self.server_id:
            raise ZoteroError(
                "The running Zotero does not provide a server ID; Zotero 10 or later is required"
            )

    def authorize(self) -> None:
        if not self.server_id:
            raise ZoteroError("Connect to Zotero before requesting write permission")
        print(
            "Approve the write request in Zotero. Choose 'Always Allow' to avoid "
            "a new prompt for each write.",
            file=sys.stderr,
        )
        body = json.dumps({"appName": APP_NAME}).encode("utf-8")
        request = Request(
            self._url("/local/authorize"),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Zotero-API-Version": "3",
                "Zotero-Server-ID": self.server_id,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(self.timeout, 120)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ZoteroError(
                f"Zotero write authorization failed with HTTP {exc.code}: {detail}"
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise ZoteroError(f"Zotero write authorization failed: {exc}") from exc
        self.api_key = str(payload.get("key", ""))
        if not self.api_key:
            raise ZoteroError("Zotero did not return a local API key")

    def get_json(self, path: str) -> Any:
        return self._request("GET", path).json()

    def new_item_template(
        self, item_type: str, *, link_mode: Optional[str] = None
    ) -> dict[str, Any]:
        """Build a template from the live schema.

        Zotero's hosted API exposes ``/items/new``, but some Local API builds
        do not. ``/itemTypeFields`` is available locally and keeps this code
        compatible with the schema of the running Zotero instance.
        """
        fields = self.get_json(
            f"/itemTypeFields?itemType={quote(item_type, safe='')}"
        )
        template: dict[str, Any] = {
            "itemType": item_type,
            "tags": [],
            "collections": [],
            "relations": {},
        }
        for entry in fields or []:
            field_name = str(entry.get("field", ""))
            if field_name:
                template[field_name] = ""
        if item_type not in {"attachment", "note", "annotation"}:
            template["creators"] = []
        if link_mode:
            template["linkMode"] = link_mode
        return template

    def write_json(
        self,
        method: str,
        path: str,
        payload: Any,
        *,
        headers: Optional[dict[str, str]] = None,
    ) -> ZoteroResponse:
        return self._request(
            method,
            path,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            write=True,
        )

    def create_item(self, item: dict[str, Any]) -> str:
        response = self.write_json(
            "POST",
            "/users/0/items",
            [item],
            headers={"Zotero-Write-Token": uuid.uuid4().hex},
        )
        payload = response.json() or {}
        successes = payload.get("successful") or payload.get("success") or {}
        created = successes.get("0") or successes.get(0)
        if isinstance(created, dict):
            created = created.get("key") or created.get("data", {}).get("key")
        if not created:
            failures = payload.get("failed") or {}
            raise ZoteroError(f"Zotero did not create the item: {failures or payload}")
        return str(created)

    def patch_item_fields(
        self, item: dict[str, Any], fields: dict[str, Any]
    ) -> None:
        data = item.get("data", item)
        key = str(data.get("key", ""))
        version = data.get("version")
        if not key or version is None:
            raise ZoteroError("Existing Zotero item has no key or version")
        response = self.write_json(
            "PATCH",
            f"/users/0/items/{quote(key)}",
            fields,
            headers={"If-Unmodified-Since-Version": str(version)},
        )
        data.update(fields)
        new_version = response.headers.get("Last-Modified-Version")
        if new_version:
            data["version"] = int(new_version)

    def upload_pdf(self, parent_key: str, pdf: Path) -> str:
        template = self.new_item_template(
            "attachment", link_mode="imported_file"
        )
        template.update(
            {
                "parentItem": parent_key,
                "linkMode": "imported_file",
                "title": pdf.name,
                "contentType": mimetypes.guess_type(pdf.name)[0]
                or "application/pdf",
                "filename": pdf.name,
            }
        )
        attachment_key = self.create_item(template)

        content = pdf.read_bytes()
        digest = hashlib.md5(content).hexdigest()  # Zotero's upload protocol uses MD5.
        file_data = urlencode(
            {
                "md5": digest,
                "filename": pdf.name,
                "filesize": len(content),
                "mtime": int(pdf.stat().st_mtime * 1000),
            }
        ).encode("ascii")
        file_path = f"/users/0/items/{quote(attachment_key)}/file"
        authorization = self._request(
            "POST",
            file_path,
            body=file_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "If-None-Match": "*",
            },
            write=True,
        ).json()
        if authorization.get("exists"):
            return attachment_key

        upload_url = str(authorization.get("url", ""))
        upload_key = str(authorization.get("uploadKey", ""))
        if not upload_url or not upload_key:
            raise ZoteroError(f"Invalid Zotero upload authorization: {authorization}")
        prefix = str(authorization.get("prefix", "")).encode("utf-8")
        suffix = str(authorization.get("suffix", "")).encode("utf-8")
        upload_request = Request(
            urljoin(f"{self.base_url}/", upload_url),
            data=prefix + content + suffix,
            headers={
                "Content-Type": authorization.get(
                    "contentType", "application/octet-stream"
                )
            },
            method="POST",
        )
        try:
            with urlopen(upload_request, timeout=max(self.timeout, 120)) as response:
                response.read()
                if response.status != 201:
                    raise ZoteroError(
                        f"Zotero file upload returned HTTP {response.status}"
                    )
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ZoteroError(
                f"Zotero file upload failed with HTTP {exc.code}: {detail}"
            ) from exc

        self._request(
            "POST",
            file_path,
            body=urlencode({"upload": upload_key}).encode("ascii"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "If-None-Match": "*",
            },
            write=True,
        )
        return attachment_key


def quartile_label(result: jrl.Result) -> str:
    if result.quartile:
        return result.quartile
    if result.doi and result.journal:
        return "Unranked"
    return "Unresolved"


def merge_tags(
    existing: list[dict[str, Any]], requested: list[str]
) -> list[dict[str, Any]]:
    """Add requested manual tags without removing or duplicating existing tags."""
    merged = [dict(tag) for tag in (existing or []) if str(tag.get("tag", "")).strip()]
    seen = {str(tag["tag"]).strip().casefold() for tag in merged}
    for value in requested:
        tag_name = value.strip()
        if tag_name and tag_name.casefold() not in seen:
            merged.append({"tag": tag_name})
            seen.add(tag_name.casefold())
    return merged


def merge_rank_tag(
    existing: list[dict[str, Any]], quartile: str
) -> list[dict[str, Any]]:
    """Replace an existing Rank tag when a Q1-Q4 or NA result is available."""
    if not re.fullmatch(r"Q[1-4]|NA", quartile or "", flags=re.IGNORECASE):
        return [dict(tag) for tag in (existing or [])]
    merged = [
        dict(tag)
        for tag in (existing or [])
        if not str(tag.get("tag", "")).strip().casefold().startswith("rank:")
    ]
    merged.append({"tag": f"Rank:{quartile.upper()}"})
    return merged


def fetch_crossref_record(
    doi: str, *, timeout: int, user_agent: str, mailto: Optional[str]
) -> dict[str, Any]:
    """Return the full Crossref record when available for richer Zotero data."""
    path = f"/works/{quote(doi, safe='')}"
    try:
        body = jrl._request_bytes(
            jrl._crossref_url(path, mailto),
            timeout=timeout,
            user_agent=user_agent,
        )
        return json.loads(body.decode("utf-8")).get("message", {})
    except Exception:
        return {}


def clean_abstract(value: Any) -> str:
    """Convert Crossref's JATS/HTML abstract into plain text for Zotero."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([.,;:!?])", r"\1", text)


def fetch_abstract(
    doi: str,
    record: dict[str, Any],
    *,
    timeout: int,
    user_agent: str,
) -> str:
    """Use Crossref's abstract, with Semantic Scholar as a metadata fallback."""
    abstract = clean_abstract(record.get("abstract", ""))
    if abstract or not doi:
        return abstract
    url = (
        f"{jrl.SEMANTIC_SCHOLAR_BASE}/paper/DOI:{quote(doi, safe='')}"
        "?fields=abstract"
    )
    try:
        payload = json.loads(
            jrl._request_bytes(
                url, timeout=timeout, user_agent=user_agent
            ).decode("utf-8")
        )
        return clean_abstract(payload.get("abstract", ""))
    except Exception:
        return ""


def abstract_from_text(text: str) -> str:
    """Extract an Abstract section from publisher PDF text."""
    if not text:
        return ""
    start = re.search(
        r"(?im)^[ \t]*a[ \t]*b[ \t]*s[ \t]*t[ \t]*r[ \t]*a[ \t]*c[ \t]*t"
        r"[ \t]*(?:[:\-–—][ \t]*)?",
        text,
    )
    if not start:
        return ""
    remainder = text[start.end() :]
    end = re.search(
        r"(?im)^[ \t]*(?:\d+(?:\.\d+)*[.)]?[ \t]+)?"
        r"(?:introduction|keywords?|index terms)\b",
        remainder,
    )
    if end:
        remainder = remainder[: end.start()]
    # Repair words split by publisher line wrapping (for example, "un-\nstable").
    remainder = remainder.replace("\u00ad", "")
    remainder = re.sub(r"(?<=\w)-[ \t]*\n[ \t]*(?=[a-z])", "", remainder)
    abstract = re.sub(r"\s+", " ", remainder).strip(" :-–—\n\t")
    if len(abstract) < 80:
        return ""
    return abstract


def fetch_pdf_abstract(path: Path, *, opening_pages: int = 3) -> str:
    """Extract an abstract from the opening pages when APIs do not supply one."""
    try:
        reader = rpf.PdfReader(str(path))
        text = "\n".join(
            reader.pages[index].extract_text() or ""
            for index in range(min(max(1, opening_pages), len(reader.pages)))
        )
    except Exception:
        return ""
    return abstract_from_text(text)


def crossref_creators(record: dict[str, Any]) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for author in record.get("author") or []:
        first_name = str(author.get("given", "")).strip()
        last_name = str(author.get("family", "")).strip()
        if first_name or last_name:
            creators.append(
                {
                    "creatorType": "author",
                    "firstName": first_name,
                    "lastName": last_name,
                }
            )
    return creators


def make_zotero_item(
    template: dict[str, Any],
    result: jrl.Result,
    record: dict[str, Any],
    *,
    title_fallback: str,
    collection_key: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    item = dict(template)
    fields: dict[str, Any] = {
        "itemType": "journalArticle",
        "title": result.paper_title or title_fallback,
        "creators": crossref_creators(record),
        "publicationTitle": result.journal,
        "abstractNote": str(record.get("_resolved_abstract", "")),
        "date": str(result.publication_year or ""),
        "volume": str(record.get("volume", "")),
        "issue": str(record.get("issue", "")),
        "pages": str(record.get("page", "")),
        "DOI": result.doi,
        "url": str(
            record.get("URL", f"https://doi.org/{result.doi}" if result.doi else "")
        ),
        "ISSN": ", ".join(
            str(value) for value in (record.get("ISSN") or [])
        ),
        "tags": merge_rank_tag(
            merge_tags(item.get("tags", []), tags or []), result.quartile
        ),
    }
    if collection_key:
        fields["collections"] = [collection_key]
    for key, value in fields.items():
        if key in item or key in {"itemType", "collections", "tags"}:
            item[key] = value
    return item


def item_data(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("data", item)


def index_existing_items(
    items: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_doi: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    for item in items:
        data = item_data(item)
        doi = jrl.normalize_doi(str(data.get("DOI", "")))
        title = jrl.normalize_title(str(data.get("title", "")))
        if doi:
            by_doi.setdefault(doi, item)
        if title:
            by_title.setdefault(title, item)
    return by_doi, by_title


def has_pdf_attachment(
    client: ZoteroLocalClient, parent_key: str, pdf: Path
) -> bool:
    children = client.get_json(f"/users/0/items/{quote(parent_key)}/children")
    for child in children or []:
        data = item_data(child)
        if data.get("itemType") != "attachment":
            continue
        if str(data.get("filename", "")).casefold() == pdf.name.casefold():
            return True
    return False


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
            "Rank PDFs with SCImago, import them into Zotero, and put the "
            "journal quartile in a Rank:Q1-style tag."
        )
    )
    parser.add_argument("--folder", type=Path, default=rpf.DEFAULT_FOLDER)
    parser.add_argument("--recursive", action="store_true")
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
    parser.add_argument(
        "--collection-key",
        help="Optional existing Zotero collection key for newly created items",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="TAG",
        help="Zotero tag to add; repeat this option to add multiple tags",
    )
    parser.add_argument("--mailto", help="Email for Crossref's polite API pool")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--opening-pages", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--no-attachments", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"error: PDF folder does not exist: {folder}", file=sys.stderr)
        return 2
    pattern = "**/*.pdf" if args.recursive else "*.pdf"
    pdfs = sorted(folder.glob(pattern), key=lambda path: str(path).casefold())
    if not pdfs:
        print(f"error: no PDF files found in {folder}", file=sys.stderr)
        return 2

    user_agent = "scimago-zotero-importer/1.0 (+https://www.zotero.org/)"
    if args.mailto:
        user_agent = f"scimago-zotero-importer/1.0 (mailto:{args.mailto})"
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

    client: Optional[ZoteroLocalClient] = None
    existing_by_doi: dict[str, dict[str, Any]] = {}
    existing_by_title: dict[str, dict[str, Any]] = {}
    template: dict[str, Any] = {}
    if not args.dry_run:
        client = ZoteroLocalClient(
            args.zotero_url, timeout=args.timeout, api_key=args.api_key
        )
        try:
            client.connect()
            existing = client.get_json(
                "/users/0/items/top?itemType=journalArticle&format=json"
            )
            existing_by_doi, existing_by_title = index_existing_items(existing)
            template = client.new_item_template("journalArticle")
        except ZoteroError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    rows: list[dict[str, Any]] = []
    failures = 0
    for number, pdf in enumerate(pdfs, 1):
        if number > 1 and args.delay:
            time.sleep(args.delay)
        result: Optional[jrl.Result] = None
        action = ""
        message = ""
        item_key = ""
        fallback_title = rpf.title_from_filename(pdf)
        try:
            doi, fallback_title, evidence = rpf.extract_pdf_doi(
                pdf,
                opening_pages=max(1, args.opening_pages),
                timeout=args.timeout,
                user_agent=user_agent,
                mailto=args.mailto,
            )
            notes: list[str] = []
            if doi:
                result = jrl.rank_input(
                    doi,
                    index=rank_index,
                    ranking_year=args.rank_year,
                    timeout=args.timeout,
                    user_agent=user_agent,
                    mailto=args.mailto,
                    elsevier_api_key=None,
                )
                if not result.doi:
                    notes.append(result.message or "journal metadata unresolved")
            else:
                notes.append(f"DOI unresolved: {evidence}")
                result = jrl.Result(
                    input=str(pdf),
                    paper_title=fallback_title,
                    ranking_year=args.rank_year,
                    status="error",
                    message=notes[-1],
                )

            if args.dry_run:
                action = "Would import/update"
                notes.append("No Zotero changes made")
            else:
                assert client is not None
                normalized_title = jrl.normalize_title(
                    result.paper_title or fallback_title
                )
                existing_item = existing_by_doi.get(result.doi) if result.doi else None
                if existing_item is None and normalized_title:
                    existing_item = existing_by_title.get(normalized_title)

                quartile = quartile_label(result)
                record = (
                    fetch_crossref_record(
                        result.doi,
                        timeout=args.timeout,
                        user_agent=user_agent,
                        mailto=args.mailto,
                    )
                    if result.doi
                    else {}
                )
                record["_resolved_abstract"] = fetch_abstract(
                    result.doi,
                    record,
                    timeout=args.timeout,
                    user_agent=user_agent,
                )
                if not record["_resolved_abstract"]:
                    record["_resolved_abstract"] = fetch_pdf_abstract(
                        pdf, opening_pages=max(1, args.opening_pages)
                    )
                if existing_item:
                    data = item_data(existing_item)
                    item_key = str(data.get("key", ""))
                    new_tags = merge_rank_tag(
                        merge_tags(data.get("tags", []), args.tag),
                        result.quartile,
                    )
                    changed_fields: dict[str, Any] = {}
                    if new_tags != data.get("tags", []):
                        changed_fields["tags"] = new_tags
                    if (
                        not str(data.get("abstractNote", "")).strip()
                        and record["_resolved_abstract"]
                    ):
                        changed_fields["abstractNote"] = record["_resolved_abstract"]
                    if changed_fields:
                        client.patch_item_fields(existing_item, changed_fields)
                    action = "Updated existing item"
                else:
                    new_item = make_zotero_item(
                        template,
                        result,
                        record,
                        title_fallback=fallback_title,
                        collection_key=args.collection_key,
                        tags=args.tag,
                    )
                    item_key = client.create_item(new_item)
                    action = "Created item"
                    stub = {"data": {**new_item, "key": item_key}}
                    if result.doi:
                        existing_by_doi[result.doi] = stub
                    if normalized_title:
                        existing_by_title[normalized_title] = stub

                if not args.no_attachments:
                    if has_pdf_attachment(client, item_key, pdf):
                        notes.append("Matching PDF attachment already exists")
                    else:
                        client.upload_pdf(item_key, pdf)
                        notes.append("PDF uploaded")
            message = "; ".join(notes)
        except Exception as exc:
            failures += 1
            action = "Failed"
            message = str(exc)

        label = quartile_label(result) if result else "Unresolved"
        print(
            f"[{number}/{len(pdfs)}] {pdf.name}: {action} ({label})"
            + (f" — {message}" if message else ""),
            file=sys.stderr,
        )
        rows.append(
            {
                "PDF": str(pdf),
                "Paper title": result.paper_title if result else fallback_title,
                "DOI": result.doi if result else "",
                "Journal": result.journal if result else "",
                "SCImago ranking year": args.rank_year,
                "Journal quartile": label,
                "Zotero item key": item_key,
                "Action": action,
                "Message": message,
            }
        )

    report = (args.report or folder / "zotero_upload_report.csv").expanduser().resolve()
    write_report(rows, report)
    print(f"Report: {report}", file=sys.stderr)
    print(f"Ranking data: {sjr_source}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
