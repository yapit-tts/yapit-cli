"""Fetch clean markdown from yapit.md documents, URLs, and local files.

Auth: set YAPIT_EMAIL and YAPIT_PASSWORD (or --email/--password).
Shared documents can be fetched without auth. Creating documents
or accessing private docs requires a yapit.md account.

Examples:

    yapit fetch https://example.com/article
    yapit fetch paper.pdf --ai -o .
    yapit list
    yapit list --json | jq '.[].title'
"""

from __future__ import annotations

import itertools
import json as json_mod
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
import tyro
from tyro.extras import SubcommandApp

# Stack Auth public credentials (baked into the frontend bundle)
_STACK_PROJECT_ID = "6038930b-72c1-407f-9e38-f1287a4d1ede"
_STACK_CLIENT_KEY = "pck_m04c3bgjsmstpk4khbhtma5161b694zcrk94v6dcavpbr"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_YAPIT_LISTEN_RE = re.compile(r"yapit\.md/listen/([0-9a-f-]{36})", re.IGNORECASE)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_PAGE_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")

app = SubcommandApp()


def _parse_pages(spec: str) -> list[int]:
    """Parse a human-friendly page spec like '1-5,8,12' into 0-indexed page indices."""
    indices: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        m = _PAGE_RANGE_RE.match(part)
        if not m:
            _die(f"invalid page spec: {part!r} (expected e.g. '1-5,8,12')")
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if start < 1:
            _die("page numbers start at 1")
        if end < start:
            _die(f"invalid page range: {start}-{end}")
        indices.extend(range(start - 1, end))
    return sorted(set(indices))


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _die(msg: str) -> None:
    _err(f"error: {msg}")
    sys.exit(1)


def _raise_for_status(resp: httpx.Response) -> None:
    """Like resp.raise_for_status(), but prints the server's error detail."""
    if resp.is_success:
        return
    detail = None
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail")
    except Exception:
        pass
    if detail:
        _die(detail)
    resp.raise_for_status()


# --- Auth ---


def _resolve_auth(email: str, password: str, base_url: str) -> tuple[str, str, str]:
    """Resolve auth and base_url from args/env, return (base_url, email, password)."""
    base_url = (base_url or os.environ.get("YAPIT_BASE_URL", "https://yapit.md")).rstrip("/")
    email = email or os.environ.get("YAPIT_EMAIL", "")
    password = password or os.environ.get("YAPIT_PASSWORD", "")
    return base_url, email, password


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
    _raise_for_status(resp)
    return resp.json()["access_token"]


def _require_auth(email: str, password: str, base_url: str) -> str:
    if not email or not password:
        _die("authentication required — set YAPIT_EMAIL and YAPIT_PASSWORD")
    return authenticate(base_url, email, password)


# --- Input resolution ---


def resolve_input(url_or_id: str) -> tuple[Literal["uuid", "url", "file", "text"], str]:
    """Classify input as a yapit document UUID, external URL, local file, or text."""
    if _UUID_RE.match(url_or_id):
        return "uuid", url_or_id

    m = _YAPIT_LISTEN_RE.search(url_or_id)
    if m:
        return "uuid", m.group(1)

    path = Path(url_or_id)
    if path.exists() and path.is_file():
        return "file", str(path.resolve())

    parsed = urlparse(url_or_id)
    if parsed.scheme in ("http", "https"):
        return "url", url_or_id
    if "." in url_or_id.split("/")[0]:
        return "url", f"https://{url_or_id}"

    if url_or_id == "-":
        return "text", sys.stdin.read()

    _die(f"cannot resolve input: {url_or_id!r} (not a UUID, URL, or file path)")
    raise AssertionError


# --- Document creation ---


def create_from_url(
    client: httpx.Client, url: str, ai: bool, pages: list[int] | None = None,
) -> tuple[str, str | None]:
    """Create a document from an external URL. Returns (doc_id, title)."""
    resp = client.post("/v1/documents/prepare", json={"url": url}, timeout=30)
    _raise_for_status(resp)
    prep = resp.json()

    doc_hash = prep["hash"]
    endpoint = prep["endpoint"]
    title = prep["metadata"].get("title")
    content_hash = prep["content_hash"]

    _err(f"Creating document from {endpoint}...")

    if endpoint == "website":
        if pages:
            _err("warning: --pages ignored for website URLs")
        resp = client.post("/v1/documents/website", json={"hash": doc_hash}, timeout=60)
        _raise_for_status(resp)
        data = resp.json()
        return data["id"], data.get("title") or title

    if endpoint == "document":
        body: dict = {"hash": doc_hash, "ai_transform": ai, "batch_mode": False}
        if pages is not None:
            body["pages"] = pages
        resp = client.post("/v1/documents/document", json=body, timeout=60)
        _raise_for_status(resp)

        if resp.status_code == 201:
            data = resp.json()
            return data["id"], data.get("title") or title

        extraction = resp.json()
        poll_pages = pages if pages is not None else list(range(extraction["total_pages"]))
        return _poll_extraction(
            client, extraction.get("extraction_id"), content_hash, ai, poll_pages, title
        )

    _die(f"unexpected endpoint type: {endpoint}")
    raise AssertionError


def create_from_file(
    client: httpx.Client, file_path: str, ai: bool, pages: list[int] | None = None,
) -> tuple[str, str | None]:
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
    _raise_for_status(resp)
    prep = resp.json()

    doc_hash = prep["hash"]
    endpoint = prep["endpoint"]
    title = prep["metadata"].get("title")
    content_hash = prep["content_hash"]

    _err(f"Creating document from {endpoint}...")

    if endpoint == "document":
        body: dict = {"hash": doc_hash, "ai_transform": ai, "batch_mode": False}
        if pages is not None:
            body["pages"] = pages
        resp = client.post("/v1/documents/document", json=body, timeout=60)
        _raise_for_status(resp)

        if resp.status_code == 201:
            data = resp.json()
            return data["id"], data.get("title") or title

        extraction = resp.json()
        poll_pages = pages if pages is not None else list(range(extraction["total_pages"]))
        return _poll_extraction(
            client, extraction.get("extraction_id"), content_hash, ai, poll_pages, title
        )

    if endpoint == "website":
        if pages:
            _err("warning: --pages ignored for website files")
        resp = client.post("/v1/documents/website", json={"hash": doc_hash}, timeout=60)
        _raise_for_status(resp)
        data = resp.json()
        return data["id"], data.get("title") or title

    if endpoint == "text":
        if pages:
            _err("warning: --pages ignored for text files")
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
    _raise_for_status(resp)
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
        _raise_for_status(resp)
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
    _raise_for_status(resp)
    raise AssertionError  # unreachable


def fetch_document_metadata(base_url: str, doc_id: str, token: str | None) -> tuple[str | None, str | None]:
    """Fetch document title and source URL from the API."""
    url = f"{base_url}/api/v1/documents/{doc_id}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.get(url, headers=headers, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        title = data.get("title")
        source_url = (data.get("metadata_dict") or {}).get("url")
        return title, source_url
    return None, None


# --- Save to directory ---


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-").lower()
    return slug[:100] or "untitled"


def _make_image_downloader(slug: str, base_url: str, doc_dir: Path) -> Callable[[str], str]:
    """Create a reusable image downloader that renames images to <slug>-<N>.<ext>.

    Returns a function that processes markdown, downloading and renaming images.
    State (counter, seen URLs) is shared across calls so the same image gets the
    same filename when referenced in both the main and annotated markdown.
    """
    seen: dict[str, str] = {}
    counter = itertools.count(1)

    def download_images(markdown: str) -> str:
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
            orig_name = Path(parsed.path).name
            if not orig_name:
                return match.group(0)

            ext = Path(orig_name).suffix or ".png"
            filename = f"{slug}-{next(counter)}{ext}"

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

    return download_images


def _yaml_frontmatter(title: str | None, source_url: str | None) -> str:
    """Build YAML frontmatter string. Returns empty string if no metadata."""
    fields: list[str] = []
    if title:
        escaped = title.replace('"', '\\"')
        fields.append(f'title: "{escaped}"')
    if source_url:
        fields.append(f"source: {source_url}")
    if not fields:
        return ""
    return "---\n" + "\n".join(fields) + "\n---\n\n"


def save_to_directory(
    md: str,
    annotated_md: str | None,
    title: str | None,
    base_url: str,
    output_dir: Path,
    source_url: str | None = None,
    download_images: bool = True,
    name: str | None = None,
) -> Path:
    """Save markdown, annotated version, and images to a directory. Returns the path."""
    slug = _slugify(name or title or "untitled")
    doc_dir = output_dir / slug

    if doc_dir.exists():
        _die(f"output directory already exists: {doc_dir}")

    doc_dir.mkdir(parents=True)

    if download_images:
        dl = _make_image_downloader(slug, base_url, doc_dir)
        md = dl(md)
        if annotated_md:
            annotated_md = dl(annotated_md)

    frontmatter = _yaml_frontmatter(title, source_url)
    (doc_dir / f"{slug}.md").write_text(frontmatter + md, encoding="utf-8")
    if annotated_md:
        (doc_dir / "TTS.md").write_text(annotated_md, encoding="utf-8")

    return doc_dir


# --- Commands ---


@dataclass
class FetchArgs:
    """Fetch clean markdown from a URL, file, UUID, or stdin.

    Markdown goes to stdout, progress/errors to stderr. Pipe-friendly.

    -o saves to <dir>/<slug>/ containing <slug>.md, TTS.md, and images.
    Prints the output path to stdout. Errors if the directory exists.

    Examples:

        yapit fetch https://example.com/article
        yapit fetch paper.pdf --ai -o .
        yapit fetch 550e8400-e29b-41d4-a716-446655440000 --annotated
        echo "hello world" | yapit fetch -
    """

    input: Annotated[str, tyro.conf.Positional]
    """URL, file path, yapit document UUID, or yapit.md/listen/... link. Use "-" for stdin."""

    annotated: bool = False
    """Include TTS annotations (yap-speak, yap-show, yap-cap tags)."""

    output_dir: Annotated[str | None, tyro.conf.arg(aliases=["-o"])] = None
    """Save markdown, TTS annotations, and images to <output-dir>/<slug>/."""

    name: Annotated[str | None, tyro.conf.arg(aliases=["-n"])] = None
    """With -o: override the directory and file name (default: slugified title)."""

    images: bool = True
    """With -o: download images. Use --no-images to skip."""

    tts: bool = True
    """With -o: save TTS.md (annotated version). Use --no-tts to skip."""

    pages: Annotated[str | None, tyro.conf.arg(aliases=["-p"])] = None
    """Pages to extract from PDFs (1-indexed, inclusive). Single: '5'. Range: '1-5'. List: '1,3,7-10'. Default: all."""

    ai: bool = False
    """Use AI extraction for PDFs (uses quota)."""

    base_url: str = ""
    """Yapit instance URL. Default: https://yapit.md. Env: YAPIT_BASE_URL."""

    email: str = ""
    """Auth email. Env: YAPIT_EMAIL."""

    password: str = ""
    """Auth password. Env: YAPIT_PASSWORD."""


@app.command(name="fetch")
def cmd_fetch(args: FetchArgs) -> None:
    """Fetch clean markdown from a URL, file, UUID, or stdin."""
    base_url, email, password = _resolve_auth(args.email, args.password, args.base_url)

    input_type, value = resolve_input(args.input)
    page_indices = _parse_pages(args.pages) if args.pages else None
    token: str | None = None
    doc_id: str = ""
    title: str | None = None
    source_url: str | None = None

    if input_type == "uuid":
        if page_indices:
            _err("warning: --pages ignored when fetching an existing document")
        doc_id = value
        if email and password:
            token = authenticate(base_url, email, password)

    elif input_type == "url":
        token = _require_auth(email, password, base_url)
        client = httpx.Client(base_url=f"{base_url}/api", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        doc_id, title = create_from_url(client, value, ai=args.ai, pages=page_indices)
        source_url = value
        _err(f"Document created: {base_url}/listen/{doc_id}")

    elif input_type == "file":
        token = _require_auth(email, password, base_url)
        client = httpx.Client(base_url=f"{base_url}/api", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        doc_id, title = create_from_file(client, value, ai=args.ai, pages=page_indices)
        _err(f"Document created: {base_url}/listen/{doc_id}")

    elif input_type == "text":
        if page_indices:
            _err("warning: --pages ignored for text input")
        token = _require_auth(email, password, base_url)
        client = httpx.Client(base_url=f"{base_url}/api", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        doc_id, title = _create_text(client, value)
        _err(f"Document created: {base_url}/listen/{doc_id}")

    else:
        raise AssertionError(f"unexpected input type: {input_type}")

    if not title or not source_url:
        api_title, api_source_url = fetch_document_metadata(base_url, doc_id, token)
        if not title:
            title = api_title
        if not source_url:
            source_url = api_source_url
    md = fetch_markdown(base_url, doc_id, annotated=False, token=token)

    if args.output_dir is not None:
        annotated_md = None if not args.tts else fetch_markdown(base_url, doc_id, annotated=True, token=token)
        doc_dir = save_to_directory(
            md, annotated_md, title, base_url, Path(args.output_dir),
            source_url=source_url, download_images=args.images, name=args.name,
        )
        print(doc_dir)
    else:
        if args.annotated:
            md = fetch_markdown(base_url, doc_id, annotated=True, token=token)
        frontmatter = _yaml_frontmatter(title, source_url)
        print(frontmatter + md, end="" if md.endswith("\n") else "\n")


@dataclass
class ListArgs:
    """List your documents with titles and URLs.

    JSON schema (--json):

        [{"id": "uuid", "title": "str|null", "url": "str", "created": "iso8601", "public": bool}]

    Examples:

        yapit list
        yapit list --json | jq '.[] | select(.title | test("arxiv"; "i"))'
        yapit list --limit 200
    """

    json: bool = False
    """Emit JSON to stdout."""

    limit: int = 50
    """Max documents to fetch (0 = all)."""

    base_url: str = ""
    """Yapit instance URL. Default: https://yapit.md. Env: YAPIT_BASE_URL."""

    email: str = ""
    """Auth email. Env: YAPIT_EMAIL."""

    password: str = ""
    """Auth password. Env: YAPIT_PASSWORD."""


@app.command(name="list")
def cmd_list(args: ListArgs) -> None:
    """List your documents with titles and URLs."""
    base_url, email, password = _resolve_auth(args.email, args.password, args.base_url)
    token = _require_auth(email, password, base_url)

    docs: list[dict] = []
    offset = 0
    page_size = min(args.limit, 100) if args.limit > 0 else 100

    while True:
        resp = httpx.get(
            f"{base_url}/api/v1/documents",
            headers={"Authorization": f"Bearer {token}"},
            params={"offset": offset, "limit": page_size},
            timeout=15,
        )
        _raise_for_status(resp)
        page = resp.json()
        if not page:
            break
        docs.extend(page)
        if len(page) < page_size:
            break
        if args.limit > 0 and len(docs) >= args.limit:
            docs = docs[: args.limit]
            break
        offset += len(page)

    rows = [
        {
            "id": d["id"],
            "title": d["title"],
            "url": f"{base_url}/listen/{d['id']}",
            "created": d["created"],
            "public": d["is_public"],
        }
        for d in docs
    ]

    if args.json:
        print(json_mod.dumps(rows, indent=2))
        return

    if not rows:
        _err("No documents found.")
        return

    max_title = max(len(r["title"] or "(untitled)") for r in rows)
    max_title = min(max_title, 60)
    for r in rows:
        title = (r["title"] or "(untitled)")[:60]
        print(f"{title:<{max_title}}  {r['url']}")


def main() -> None:
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"yapit {pkg_version('yapit')}")
        sys.exit(0)

    app.cli(description=__doc__, config=(tyro.conf.OmitArgPrefixes,))


