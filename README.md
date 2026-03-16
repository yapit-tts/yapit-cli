# yapit

CLI for [yapit.md](https://yapit.md) — fetch clean markdown from URLs and documents.

## Install

```bash
uv tool install yapit
```

## Usage

```bash
# Fetch markdown of a shared document
yapit https://yapit.md/listen/<doc-id>

# Create document from URL and print markdown
yapit https://example.com/article

# With TTS annotations
yapit <doc-id> --annotated

# Archive locally with images (for Obsidian, etc.)
yapit https://arxiv.org/abs/2301.00001 --archive
```

## Auth

Creating documents or accessing private docs requires authentication:

```bash
export YAPIT_EMAIL=you@example.com
export YAPIT_PASSWORD=...
```

## Archive mode

`--archive` saves to `~/Documents/archive/papers/<slug>/`:

```
<slug>/
  <slug>.md     # clean markdown
  TTS.md        # annotated version
  *.png         # extracted images
```

Override the base directory with `YAPIT_ARCHIVE_DIR` or `--archive-dir`.
