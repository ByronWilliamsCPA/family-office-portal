# ADR-007: Live Proxy for Document Files

> **Status**: Accepted
> **Date**: 2026-10-05
> **Relates to**: [ADR-003](adr-003-backend-data-aggregation.md) (this is a
> bounded exception to it)

## TL;DR

Document *metadata* stays in the SQLite cache like every other dataset.
Document *files* are not cached: the preview and download routes stream each
file from llc-manager (`GET /api/v1/documents/{id}/file`) at request time.
These two routes are the only request handlers that call a backend. The
visibility check runs against the cache before any upstream request is made.

## Context

ADR-003 says route handlers read only from SQLite and never call a backend,
so a slow or failed backend can never break a page. That works for small
structured data. It does not fit document files:

- Caching the files would copy every family document (wills, trusts, tax
  returns) onto the portal's disk, making the portal a second document store
  with its own backup, retention and deletion duties.
- The files are large and rarely opened. Pre-fetching all of them on a
  schedule would move far more data than people ever look at.
- A preview is an explicit request for one file. If the document service is
  down, there is nothing useful to show from a cache anyway, other than an
  old copy of a document that may since have been replaced.

## Decision

Preview (`/documents/{id}/preview`) and download (`/documents/{id}/download`)
stream the file from llc-manager on each request, under these rules
(`app/document_files.py`, `app/routes/documents.py`):

1. **Visibility first.** The route looks the document up in the cache with the
   viewer's role. Unknown documents, and confidential documents requested by
   a Viewer, get 404 and no upstream request is made. Tests assert that the
   fake upstream received nothing.
2. **Backend key, never the viewer's identity.** The request carries the
   llc-manager `X-API-Key` from settings. The viewer's identity token is not
   forwarded. Redirects are not followed and proxy environment variables are
   ignored, so the key only goes to the configured URL. The document ID is percent-encoded as one path segment.
3. **Bounded.** The backend timeout (`BACKEND_TIMEOUT_SECONDS`) applies to
   connecting and to each read, and a whole transfer may take at most five
   minutes. Files over 50 MiB are refused: by the declared `Content-Length`
   before any byte is sent, or while streaming when no length is declared. A
   stream that breaks or exceeds a limit part way through is aborted, so the
   browser reports a failed download instead of keeping a truncated file. The
   upstream connection is closed however the response ends, including when
   the browser disconnects or the request is cancelled.
4. **Safe headers.** The content type comes from an allowlist (PDF, common
   images, plain text, CSV, Word and Excel); anything else, including HTML and
   SVG, is served as `application/octet-stream`. Only PDFs and images are
   shown inline; every other type downloads even from the preview route.
   `Content-Disposition` carries a cleaned ASCII `filename` plus an RFC 6266 /
   RFC 5987 `filename*`, with control and format characters removed, so a
   document title cannot inject a header. Responses also send
   `X-Content-Type-Options: nosniff` and `Cache-Control: private, no-store`.
5. **Plain errors.** Upstream 404 becomes 404. Timeouts become 504. Any other
   upstream failure, including 401 or 403 (a key problem, not a viewer
   problem), becomes 502. Upstream bodies and headers are never read into the
   response or the logs; the browser sees the portal's usual plain-English
   error text.
6. **Not connected.** With no llc-manager URL, or a URL without a usable key,
   the routes answer 503 without any outbound request, matching the ADR-003
   amendment of 2026-09-30.
7. **Page links.** A preview link may end in `#page=N`. The fragment never
   reaches the server; the browser's PDF viewer opens at that page. The
   `document_url` template filter builds these links and drops a page that is
   not a positive whole number.

## Consequences

- Preview and download depend on llc-manager being up at the moment of the
  request. Every other page still renders from the cache when it is down.
- Each open file holds one outbound connection for the length of the
  transfer. The portal runs a single worker, so very slow transfers can queue
  other requests; the size cap and per-read timeout limit this.
- Visibility follows the cache. A document newly marked confidential upstream
  stays visible to Viewers until the next documents refresh (at most 12
  hours). An admin can trigger a documents refresh to close that gap.
- #ASSUME the document service sets an accurate `Content-Type` for each file.
  #VERIFY by previewing one file of each stored type once the service is
  connected; a wrong type only downgrades the file to a download.
- No `Content-Security-Policy: sandbox` is sent: it stops the built-in PDF
  viewer of some browsers. The content-type allowlist is what keeps
  scriptable types (HTML, SVG) from rendering in the portal's origin.
- #ASSUME no stored document exceeds 50 MiB. #VERIFY against the largest file
  the document service holds before connecting it; the cap is
  `MAX_DOCUMENT_BYTES` in `app/document_files.py`.
