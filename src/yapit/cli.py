"""Fetch clean markdown from yapit.md documents and URLs.

Create documents from URLs and download their markdown, optionally with
TTS annotations. Archive locally with images for Obsidian integration.

Examples::

    yapit https://example.com/article
    yapit https://arxiv.org/abs/2301.00001
    yapit https://yapit.md/listen/550e8400-e29b-41d4-a716-446655440000
    yapit 550e8400-e29b-41d4-a716-446655440000 --annotated
    yapit https://example.com/article --archive
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
import tyro

# Stack Auth public credentials (baked into the frontend bundle)
_STACK_PROJECT_ID = "6038930b-72c1-407f-9e38-f1287a4d1ede"
_STACK_CLIENT_KEY = "pck_m04c3bgjsmstpk4khbhtma5161b694zcrk94v6dcavpbr"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_YAPIT_LISTEN_RE = re.compile(r"yapit\.md/listen/([0-9a-f-]{36})", re.IGNORECASE)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _die(msg: str) -> None:
    _err(f"error: {msg}")
    sys.exit(1)


# --- Input resolution ---


def resolve_input(url_or_id: str) -> tuple[Literal["uuid", "external"], str]:
    """Detect whether input is a yapit document UUID or an external URL."""
    if _UUID_RE.match(url_or_id):
        return "uuid", url_or_id

    m = _YAPIT_LISTEN_RE.search(url_or_id)
    if m:
        return "uuid", m.group(1)

    parsed = urlparse(url_or_id)
    if not parsed.scheme:
        url_or_id = f"https://{url_or_id}"

    return "external", url_or_id


# --- Auth ---


def authenticate(base_url: str, email: str, password: str) -> str:
    """Sign in via Stack Auth and return an access token."""
    resp = httpx.post(
        f"{base_url}/auth/api/v1/auth/password/sign-in",
        headers={
            "Content-Type": "application/json",
            "X-Stack-Access-Type": "client",
            "X-Stack-Project-Id": _STACK_PROJECT_ID,
            "X-Stack-Publishable-Client-Key": _STACK_CLIENT_KEY,
        },
        json={"email": email, "password": password},
        timeout=15,
    )
    if resp.status_code == 400:
        _die("authentication failed — check email/password")
    resp.raise_for_status()
    return resp.json()["access_token"]


# --- Document creation ---


def create_document(client: httpx.Client, url: str, ai: bool) -> tuple[str, str | None]:
    """Create a document from an external URL. Returns (doc_id, title)."""
    # Step 1: prepare
    resp = client.post("/v1/documents/prepare", json={"url": url}, timeout=30)
    resp.raise_for_status()
    prep = resp.json()

    doc_hash = prep["hash"]
    endpoint = prep["endpoint"]
    title = prep["metadata"].get("title")
    content_hash = prep["content_hash"]

    _err(f"Creating document from {endpoint}...")

    # Step 2: create
    if endpoint == "website":
        resp = client.post("/v1/documents/website", json={"hash": doc_hash}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data.get("title") or title

    if endpoint == "document":
        resp = client.post(
            "/v1/documents/document",
            json={"hash": doc_hash, "ai_transform": ai, "batch_mode": False},
            timeout=60,
        )
        resp.raise_for_status()

        if resp.status_code == 201:
            data = resp.json()
            return data["id"], data.get("title") or title

        # 202 — async extraction, need to poll
        extraction = resp.json()
        extraction_id = extraction.get("extraction_id")
        total_pages = extraction["total_pages"]
        pages = list(range(total_pages))

        return _poll_extraction(client, extraction_id, content_hash, ai, pages, title)

    if endpoint == "text":
        _die("text endpoint not supported for URL creation")

    _die(f"unexpected endpoint type: {endpoint}")
    raise AssertionError  # unreachable


def _poll_extraction(
    client: httpx.Client,
    extraction_id: str | None,
    content_hash: str,
    ai_transform: bool,
    pages: list[int],
    title: str | None,
) -> tuple[str, str | None]:
    """Poll extraction status until complete. Returns (doc_id, title)."""
    interval = 0.5
    max_interval = 3.0

    while True:
        resp = client.post(
            "/v1/documents/extraction/status",
            json={
                "extraction_id": extraction_id,
                "content_hash": content_hash,
                "ai_transform": ai_transform,
                "pages": pages,
            },
            timeout=15,
        )
        resp.raise_for_status()
        status = resp.json()

        completed = len(status.get("completed_pages", []))
        total = status["total_pages"]

        if status["status"] == "complete":
            if status.get("error"):
                _die(f"extraction failed: {status['error']}")
            doc_id = status.get("document_id")
            if not doc_id:
                _die("extraction completed but no document_id returned")
            _err(f"Extracted {total}/{total} pages")
            return doc_id, title

        _err(f"Extracting... {completed}/{total} pages")
        time.sleep(interval)
        interval = min(interval * 1.5, max_interval)


# --- Markdown fetching ---


def fetch_markdown(base_url: str, doc_id: str, annotated: bool, token: str | None) -> str:
    """Fetch markdown for a document. Auth optional — shared docs work without it."""
    suffix = "md-annotated" if annotated else "md"
    url = f"{base_url}/api/v1/documents/{doc_id}/{suffix}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    resp = httpx.get(url, headers=headers, timeout=30)
    if resp.status_code == 200:
        return resp.text
    if resp.status_code == 404:
        hint = "" if token else " (private doc? set YAPIT_EMAIL/YAPIT_PASSWORD)"
        _die(f"document {doc_id} not found{hint}")
    resp.raise_for_status()
    raise AssertionError  # unreachable


def fetch_title(base_url: str, doc_id: str, token: str | None) -> str | None:
    """Fetch document title from the API."""
    # /public endpoint always works for shared docs, no auth needed
    resp = httpx.get(f"{base_url}/api/v1/documents/{doc_id}/public", timeout=15)
    if resp.status_code == 200:
        return resp.json().get("title")
    # Private doc — need auth
    if token:
        resp = httpx.get(
            f"{base_url}/api/v1/documents/{doc_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("title")
    return None


# --- Archive ---


def archive_document(
    markdown: str,
    annotated_md: str | None,
    title: str | None,
    base_url: str,
    archive_dir: Path,
) -> Path:
    """Save markdown and images to archive directory. Returns the archive path."""
    slug = _slugify(title or "untitled")
    doc_dir = archive_dir / slug
    doc_dir.mkdir(parents=True, exist_ok=True)

    # Download images and rewrite paths
    markdown = _download_images(markdown, base_url, doc_dir)
    if annotated_md:
        annotated_md = _download_images(annotated_md, base_url, doc_dir)

    (doc_dir / f"{slug}.md").write_text(markdown, encoding="utf-8")
    if annotated_md:
        (doc_dir / "TTS.md").write_text(annotated_md, encoding="utf-8")

    return doc_dir


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return slug[:100] or "untitled"


def _download_images(markdown: str, base_url: str, doc_dir: Path) -> str:
    """Download images referenced in markdown, rewrite URLs to relative paths."""
    seen: dict[str, str] = {}

    def replace_image(match: re.Match) -> str:
        alt, url = match.group(1), match.group(2)

        if url in seen:
            return f"![{alt}]({seen[url]})"

        # Skip data URIs
        if url.startswith("data:"):
            return match.group(0)

        # Resolve relative URLs (e.g. /images/hash/file.png)
        if url.startswith("/"):
            full_url = f"{base_url}{url}"
        elif not url.startswith(("http://", "https://")):
            return match.group(0)
        else:
            full_url = url

        # Strip query params for filename
        parsed = urlparse(full_url)
        filename = Path(parsed.path).name
        if not filename:
            return match.group(0)

        try:
            resp = httpx.get(full_url, timeout=15, follow_redirects=True)
            resp.raise_for_status()
            (doc_dir / filename).write_bytes(resp.content)
            relative = f"./{filename}"
            seen[url] = relative
            return f"![{alt}]({relative})"
        except httpx.HTTPError:
            _err(f"warning: failed to download image {full_url}")
            return match.group(0)

    return _IMAGE_RE.sub(replace_image, markdown)


# --- CLI ---


@dataclass
class Args:
    """Fetch clean markdown from yapit.md documents and URLs."""

    url: Annotated[str, tyro.conf.Positional]
    """URL, yapit document UUID, or yapit.md/listen/... link."""

    annotated: bool = False
    """Include TTS annotations (yap-speak, yap-show, yap-cap tags)."""

    archive: bool = False
    """Save to archive directory with images instead of printing to stdout."""

    archive_dir: str = ""
    """Base directory for archived documents. Default: ~/Documents/archive/papers. Env: YAPIT_ARCHIVE_DIR."""

    ai: bool = False
    """Use AI extraction for PDFs (uses quota, better quality for complex layouts)."""

    base_url: str = ""
    """Yapit instance URL. Default: https://yapit.md. Env: YAPIT_BASE_URL."""

    email: str = ""
    """Auth email. Env: YAPIT_EMAIL."""

    password: str = ""
    """Auth password. Env: YAPIT_PASSWORD."""


def main() -> None:
    args = tyro.cli(Args, description=__doc__)

    base_url = (args.base_url or os.environ.get("YAPIT_BASE_URL", "https://yapit.md")).rstrip("/")
    email = args.email or os.environ.get("YAPIT_EMAIL", "")
    password = args.password or os.environ.get("YAPIT_PASSWORD", "")
    archive_dir = Path(
        args.archive_dir or os.environ.get("YAPIT_ARCHIVE_DIR", "~/Documents/archive/papers")
    ).expanduser()

    input_type, value = resolve_input(args.url)
    token: str | None = None

    # Authenticate if needed
    needs_auth = input_type == "external" or (email and password)
    if needs_auth:
        if not email or not password:
            _die("authentication required — set YAPIT_EMAIL and YAPIT_PASSWORD")
        token = authenticate(base_url, email, password)

    # Create document if external URL
    doc_id: str
    title: str | None = None
    if input_type == "external":
        assert token is not None
        client = httpx.Client(
            base_url=f"{base_url}/api",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        doc_id, title = create_document(client, value, ai=args.ai)
        _err(f"Document created: {base_url}/listen/{doc_id}")
    else:
        doc_id = value

    # Fetch markdown
    if args.archive:
        if not title:
            title = fetch_title(base_url, doc_id, token)
        md = fetch_markdown(base_url, doc_id, annotated=False, token=token)
        annotated_md = fetch_markdown(base_url, doc_id, annotated=True, token=token)
        doc_dir = archive_document(md, annotated_md, title, base_url, archive_dir)
        print(doc_dir)
    else:
        md = fetch_markdown(base_url, doc_id, annotated=args.annotated, token=token)
        print(md)
