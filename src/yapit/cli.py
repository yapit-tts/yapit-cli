"""Fetch clean markdown from yapit.md documents, URLs, and local files.

Create documents from URLs or local files and download their markdown,
optionally with TTS annotations.

Examples::

    yapit https://example.com/article
    yapit https://arxiv.org/abs/2301.00001
    yapit paper.pdf
    yapit paper.pdf --ai
    yapit 550e8400-e29b-41d4-a716-446655440000 --annotated
    yapit https://yapit.md/listen/550e8400-... -o .
    echo "hello world" | yapit -
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


def resolve_input(url_or_id: str) -> tuple[Literal["uuid", "url", "file", "text"], str]:
    """Classify input as a yapit document UUID, external URL, local file, or text."""
    if _UUID_RE.match(url_or_id):
        return "uuid", url_or_id

    m = _YAPIT_LISTEN_RE.search(url_or_id)
    if m:
        return "uuid", m.group(1)

    # Local file?
    path = Path(url_or_id)
    if path.exists() and path.is_file():
        return "file", str(path.resolve())

    # If it has a dot after the first segment, treat as URL
    parsed = urlparse(url_or_id)
    if parsed.scheme in ("http", "https"):
        return "url", url_or_id
    if "." in url_or_id.split("/")[0]:
        return "url", f"https://{url_or_id}"

    # Stdin
    if url_or_id == "-":
        return "text", sys.stdin.read()

    _die(f"cannot resolve input: {url_or_id!r} (not a UUID, URL, or file path)")
    raise AssertionError


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


def _require_auth(email: str, password: str, base_url: str) -> str:
    if not email or not password:
        _die("authentication required — set YAPIT_EMAIL and YAPIT_PASSWORD")
    return authenticate(base_url, email, password)


# --- Document creation ---


def create_from_url(client: httpx.Client, url: str, ai: bool) -> tuple[str, str | None]:
    """Create a document from an external URL. Returns (doc_id, title)."""
    resp = client.post("/v1/documents/prepare", json={"url": url}, timeout=30)
    resp.raise_for_status()
    prep = resp.json()

    doc_hash = prep["hash"]
    endpoint = prep["endpoint"]
    title = prep["metadata"].get("title")
    content_hash = prep["content_hash"]

    _err(f"Creating document from {endpoint}...")

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

        extraction = resp.json()
        return _poll_extraction(
            client, extraction.get("extraction_id"), content_hash, ai, list(range(extraction["total_pages"])), title
        )

    _die(f"unexpected endpoint type: {endpoint}")
    raise AssertionError


def create_from_file(client: httpx.Client, file_path: str, ai: bool) -> tuple[str, str | None]:
    """Create a document from a local file. Returns (doc_id, title)."""
    path = Path(file_path)
    content_type = _guess_content_type(path)

    _err(f"Uploading {path.name}...")
    with path.open("rb") as f:
        resp = client.post(
            "/v1/documents/prepare/upload",
            files={"file": (path.name, f, content_type)},
            timeout=60,
        )
    resp.raise_for_status()
    prep = resp.json()

    doc_hash = prep["hash"]
    endpoint = prep["endpoint"]
    title = prep["metadata"].get("title")
    content_hash = prep["content_hash"]

    _err(f"Creating document from {endpoint}...")

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

        extraction = resp.json()
        return _poll_extraction(
            client, extraction.get("extraction_id"), content_hash, ai, list(range(extraction["total_pages"])), title
        )

    if endpoint == "text":
        content = path.read_text(encoding="utf-8")
        return _create_text(client, content, title=path.stem)

    _die(f"unexpected endpoint type for file: {endpoint}")
    raise AssertionError


def _create_text(client: httpx.Client, content: str, title: str | None = None) -> tuple[str, str | None]:
    """Create a document from plain text/markdown. Returns (doc_id, title)."""
    resp = client.post(
        "/v1/documents/text",
        json={"content": content, "title": title},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["id"], data.get("title") or title


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".html": "text/html",
        ".htm": "text/html",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")


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
    last_completed = -1

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

        if completed != last_completed:
            _err(f"Extracting... {completed}/{total} pages")
            last_completed = completed
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
    url = f"{base_url}/api/v1/documents/{doc_id}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.get(url, headers=headers, timeout=15)
    if resp.status_code == 200:
        return resp.json().get("title")
    return None


# --- Save to directory ---


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return slug[:100] or "untitled"


def _title_from_markdown(md: str) -> str | None:
    """Extract title from first # heading as fallback."""
    m = re.search(r"^#\s+(.+)$", md, re.MULTILINE)
    return m.group(1).strip() if m else None


def _download_images(markdown: str, base_url: str, doc_dir: Path) -> str:
    """Download images referenced in markdown, rewrite URLs to relative paths."""
    seen: dict[str, str] = {}

    def replace_image(match: re.Match) -> str:
        alt, url = match.group(1), match.group(2)

        if url in seen:
            return f"![{alt}]({seen[url]})"

        if url.startswith("data:"):
            return match.group(0)

        if url.startswith("/"):
            full_url = f"{base_url}{url}"
        elif not url.startswith(("http://", "https://")):
            return match.group(0)
        else:
            full_url = url

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


def save_to_directory(
    md: str,
    annotated_md: str | None,
    title: str | None,
    base_url: str,
    output_dir: Path,
    download_images: bool = True,
) -> Path:
    """Save markdown, annotated version, and images to a directory. Returns the path."""
    slug = _slugify(title or "untitled")
    doc_dir = output_dir / slug

    if doc_dir.exists():
        _die(f"output directory already exists: {doc_dir}")

    doc_dir.mkdir(parents=True)

    if download_images:
        md = _download_images(md, base_url, doc_dir)
        if annotated_md:
            annotated_md = _download_images(annotated_md, base_url, doc_dir)

    (doc_dir / f"{slug}.md").write_text(md, encoding="utf-8")
    if annotated_md:
        (doc_dir / "TTS.md").write_text(annotated_md, encoding="utf-8")

    return doc_dir


# --- CLI ---


@dataclass
class Args:
    """Fetch clean markdown from yapit.md documents, URLs, and local files."""

    input: Annotated[str, tyro.conf.Positional]
    """URL, file path, yapit document UUID, or yapit.md/listen/... link. Use "-" for stdin."""

    annotated: bool = False
    """Include TTS annotations (yap-speak, yap-show, yap-cap tags)."""

    output_dir: Annotated[str | None, tyro.conf.arg(aliases=["-o"])] = None
    """Save markdown, TTS annotations, and images to <output-dir>/<slug>/. Prints path to stdout."""

    images: bool = True
    """With -o: download images. Use --no-images to skip."""

    tts: bool = True
    """With -o: save TTS.md (annotated version). Use --no-tts to skip."""

    ai: bool = False
    """Use AI extraction for PDFs (uses quota). Produces TTS annotations and handles complex layouts, math, figures."""

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

    input_type, value = resolve_input(args.input)
    token: str | None = None

    doc_id: str
    title: str | None = None

    if input_type == "uuid":
        doc_id = value
        if email and password:
            token = authenticate(base_url, email, password)

    elif input_type == "url":
        token = _require_auth(email, password, base_url)
        client = httpx.Client(base_url=f"{base_url}/api", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        doc_id, title = create_from_url(client, value, ai=args.ai)
        _err(f"Document created: {base_url}/listen/{doc_id}")

    elif input_type == "file":
        token = _require_auth(email, password, base_url)
        client = httpx.Client(base_url=f"{base_url}/api", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        doc_id, title = create_from_file(client, value, ai=args.ai)
        _err(f"Document created: {base_url}/listen/{doc_id}")

    elif input_type == "text":
        token = _require_auth(email, password, base_url)
        client = httpx.Client(base_url=f"{base_url}/api", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        doc_id, title = _create_text(client, value)
        _err(f"Document created: {base_url}/listen/{doc_id}")

    else:
        raise AssertionError(f"unexpected input type: {input_type}")

    if args.output_dir is not None:
        if not title:
            title = fetch_title(base_url, doc_id, token)
        md = fetch_markdown(base_url, doc_id, annotated=False, token=token)
        if not title:
            title = _title_from_markdown(md)
        annotated_md = None if not args.tts else fetch_markdown(base_url, doc_id, annotated=True, token=token)
        doc_dir = save_to_directory(
            md, annotated_md, title, base_url, Path(args.output_dir), download_images=args.images
        )
        print(doc_dir)
    else:
        md = fetch_markdown(base_url, doc_id, annotated=args.annotated, token=token)
        print(md)
