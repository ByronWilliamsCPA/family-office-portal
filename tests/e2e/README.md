# Browser checks at tablet size

These tests open every page in headless Chromium at 1024x768 (the family's
tablet size) and check layout, tap targets, text clipping, headings, labels,
JavaScript-off readability, the back button, the two-click rule and
accessibility (axe-core). They are opt-in: the default `uv run pytest` skips
them, and they never count toward coverage.

Playwright and axe-playwright-python are dev-only dependencies (the `dev`
extra). The production image installs with `--no-dev`, so neither reaches it.

## Run locally

1. `uv sync --extra dev`
2. `uv run playwright install chromium`
3. Put the standalone `tailwindcss` CLI on `PATH`. Use the version pinned in the
   `Dockerfile` (`TAILWIND_VERSION`) and check its checksum against
   `TAILWIND_SHA256` there. The built stylesheet is not committed, and a layout
   check without it means nothing.
4. `uv run pytest -m e2e --no-cov -p no:cacheprovider`

`--no-cov` is needed because the app runs in a subprocess, so the coverage
gate does not apply. If `tailwindcss` or Chromium is missing, the tests skip
with a message saying what to install. Set `E2E_REQUIRE_BROWSER_STACK=1` (CI
does) to make that a failure instead of a skip.

## What the tests start

- The real app under `uvicorn` (one worker, scheduler off) on a free localhost
  port, with a temporary SQLite file filled with made-up rows.
- A random per-run signing secret and a freshly minted token, sent in the
  `X-authentik-jwt` header. No real secret or `.env` file is read.
- The stylesheet is built into a temporary file and handed to the browser in
  place of `/static/css/output.css`; the working tree is not touched.

Full-page screenshots go to `e2e-screenshots/` (git-ignored; override with
`E2E_SCREENSHOT_DIR`). CI uploads them as an artifact so a person can look.
