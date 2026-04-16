# yapit

CLI for [yapit.md](https://yapit.md) — fetch clean markdown from URLs, files, and documents.

## Install

```bash
uv tool install yapit
```

## Usage

```bash
# Create document from URL, print markdown
yapit https://example.com/article
yapit https://arxiv.org/abs/2301.00001

# Local files
yapit paper.pdf
yapit notes.md

# With AI transformation (TTS annotations, spoken readings for math, figure extraction, noise filtering)
yapit paper.pdf --ai

# Fetch existing document
yapit https://yapit.md/listen/<doc-id>
yapit <doc-id> --annotated

# Save to directory with images
yapit <doc-id> -o .

# Stdin
echo "hello world" | yapit -
```

## Auth

Creating documents or accessing private docs requires a [yapit.md](https://yapit.md) account:

```bash
export YAPIT_EMAIL=you@example.com
export YAPIT_PASSWORD=...
```

Fetching shared documents works without auth.

## Self-hosting

Point the CLI at your own instance with `YAPIT_BASE_URL`:

```bash
export YAPIT_BASE_URL=http://localhost:8000
yapit fetch paper.pdf
```

If your server runs with `AUTH_ENABLED=false` (the `make self-host` default), no credentials are needed. If auth is enabled on your instance, set `YAPIT_EMAIL` / `YAPIT_PASSWORD` as above.

## Save to directory (`-o`)

`-o <dir>` saves markdown, TTS annotations, and images:

```
<dir>/<slug>/
  <slug>.md       # clean markdown, image paths rewritten to relative
  TTS.md          # annotated version (yap-speak, yap-show tags)
  <slug>-<N>.png  # images renamed sequentially
```

Errors if the output directory already exists.
