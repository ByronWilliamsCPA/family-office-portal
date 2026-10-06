# ADR-007: Live Proxy for Document Files

> **Status**: Accepted
> **Date**: 2026-10-05
> **Relates to**: [ADR-003](adr-003-backend-data-aggregation.md) (this is a
> bounded exception to it)

## TL;DR

Document *metadata* stays in the SQLite cache like every other dataset.
Document *files* are not cached: the preview and download routes stream each
file from llc-manager (`GET /api/v1/documents/{id}/file`) at request time.
These two routes are a bounded exception to ADR-003's rule that request
handlers never call a backend. The visibility check runs against the cache
before any upstream request is made.

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
   ignored, so the key only goes to the configured URL. The document ID is
   percent-encoded as one path segment; an ID of `.` or `..` is refused with
   404 before any request, and the documents refresh skips IDs that cannot
   form one segment.
3. **Bounded.** The backend timeout (`BACKEND_TIMEOUT_SECONDS`) applies to
   connecting and to each read. A whole response may run at most five minutes
   (`MAX_TRANSFER_SECONDS`), enforced as a time limit around the entire send,
   so an upstream that sends one byte just inside each read timeout, or a
   browser that stops reading, is still cut off. The limit includes the
   browser's own download time: 50 MiB in five minutes needs about
   1.4 Mbit/s to the viewer. Files over 50 MiB are refused: by the declared
   `Content-Length` before any byte is sent, or while streaming when no length
   is declared. A stream that breaks or exceeds a limit part way through is
   aborted, so the browser reports a failed download instead of keeping a
   truncated file. The upstream connection is closed however the response
   ends, including when the browser disconnects or the request is cancelled.
   Both caps are module constants, not settings: one deployment, one worker,
   and a security bound should not be loosened through configuration.
4. **Safe headers.** The content type comes from an allowlist (PDF, common
   images including TIFF, plain text, CSV, Word and Excel); anything else,
   including HTML and SVG, is served as `application/octet-stream`. Only PDFs
   and raster images are shown inline; every other type downloads even from
   the preview route. `Content-Disposition` carries a cleaned ASCII `filename`
   plus an RFC 6266 / RFC 5987 `filename*`, with control and format
   characters replaced by a space, so a document title cannot inject a
   header. Responses also send `X-Content-Type-Options: nosniff`,
   `Cache-Control: private, no-store`, `Content-Security-Policy:
   frame-ancestors 'self'`, `X-Frame-Options: SAMEORIGIN` and
   `Cross-Origin-Resource-Policy: same-origin`. SAMEORIGIN rather than DENY
   keeps a later in-page preview possible. No `Content-Security-Policy:
   sandbox` is sent: it stops the built-in PDF viewers of some browsers. The
   content-type allowlist is what keeps scriptable types (HTML, SVG) from
   rendering in the portal's origin; a PDF's own script runs inside the
   browser's PDF viewer, not in the portal's origin.
5. **Plain errors.** Upstream 404 becomes 404. Timeouts become 504. Any other
   upstream failure, including 401 or 403 (a key problem, not a viewer
   problem), becomes 502. Upstream bodies are never read into the response or
   the logs. Of the upstream headers, only `Content-Type` (reduced to the
   allowlist) and a plain `Content-Length` are used; no other upstream header
   is passed on or logged. The browser sees the portal's usual plain-English
   error text.
6. **Not connected.** With no llc-manager URL, or a URL without a usable key,
   the routes answer 503 without any outbound request, matching the ADR-003
   amendment of 2026-09-30. A URL or key that cannot form a request (a bad
   port, a non-ASCII key) is also 503.
7. **Page links.** A preview link may end in `#page=N`. The fragment never
   reaches the server; the browser's PDF viewer opens at that page. The
   `document_url` template filter builds these links and drops a page that is
   not a positive whole number of at most six digits.

## Consequences

- Preview and download depend on llc-manager being up at the moment of the
  request. Every other page still renders from the cache when it is down.
- Each open file holds one outbound connection for the length of the
  transfer. The proxy is async, so a slow transfer does not block other
  requests, but each one holds a connection and some memory until it ends;
  the size cap and the five-minute limit bound that. Concurrent file streams
  are not otherwise limited: the viewers are a handful of family members.
- Each file request opens a new HTTP client, so there is no connection reuse
  and, over https, a new TLS handshake per file. This is deliberate: nothing
  is shared between requests, and a file request is rare next to its size.
- No `Range` requests are forwarded and the routes answer `GET` only, so
  every preview fetches the whole file and a browser cannot resume or seek
  within a large PDF before it has loaded.
- Visibility follows the cache. A document newly marked confidential upstream
  stays visible to Viewers until the next successful documents refresh
  (scheduled every 12 hours). A refresh that fails, for example because the
  document service is down, keeps the previous cache, so the window lasts
  until a refresh succeeds; a single malformed item is skipped and logged
  rather than failing the refresh. An admin can close the gap with
  `POST /admin/refresh/documents` and confirm it in `/admin/refresh-status`.
  The upstream file endpoint does not know the viewer's role, so a live
  per-request confidentiality check would need a further metadata call to
  the document service; that is not done today.
- #ASSUME the document service sets an accurate `Content-Type` for each file.
  #VERIFY by previewing one file of each stored type once the service is
  connected; a wrong type only downgrades the file to a download.
- #ASSUME `Cross-Origin-Resource-Policy: same-origin` does not stop any
  browser's PDF viewer opening a top-level preview. #VERIFY by opening an
  inline PDF in Chrome, Firefox and Safari before connecting the document
  service; drop the header if any viewer breaks.
- #ASSUME no stored document exceeds 50 MiB. #VERIFY against the largest file
  the document service holds before connecting it; the cap is
  `MAX_DOCUMENT_BYTES` in `app/document_files.py`.
- #ASSUME five minutes is enough for the largest file over the slowest real
  remote path. #VERIFY by downloading the largest stored file on a remote
  tablet; the limit is `MAX_TRANSFER_SECONDS` in `app/document_files.py`.
