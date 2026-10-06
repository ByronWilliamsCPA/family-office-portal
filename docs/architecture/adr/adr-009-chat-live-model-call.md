# ADR-009: Chat Is a Live Model Call, an Exception to the Cached-Read Rule

> **Status**: Accepted
> **Date**: 2026-10-05

## TL;DR

Chat answers questions about the family's documents and balances. Each
question makes two live calls while the request is open: the internal search
function (ADR-008) and the chat model. This is the one exception to ADR-003,
under which route handlers read only the SQLite cache. Everything else about
chat keeps ADR-003's spirit: balances come from the cache, nothing is stored,
a failure is one plain sentence, and chat is off ("not connected") unless it
is configured.

## Context

ADR-003 keeps every page fast and available by reading only the local SQLite
cache; scheduled jobs call the backends. A question cannot be answered ahead
of time, so chat cannot follow that rule: the passages and the answer depend
on the question.

Constraints:

- The chat model is an OpenAI-compatible `POST /v1/chat/completions` service
  with two parallel slots, a bearer key, no streaming in this version, and a
  full-answer target under 30 seconds measured non-streamed.
- The model can be steered by text it reads. Retrieved passages are
  untrusted, and so is the model's answer.
- A client-supplied `chat_template_kwargs` can turn the model's thinking back
  on, which breaks the latency target.
- Balances shown to a person must never come from model text.
- Primary users are on tablets and need plain sentences, never a stack trace.

## Decision

### The exception

`POST /chat/ask` (`app/routes/chat.py`) is the only route that calls services
while serving a request. It calls `SearchService.search_async` (ADR-008) and
the chat model (`app/chat/client.py`). Balances still come from the cache
(`account_balances` and `balances_daily`).

### Settings

All optional, named only in `app/chat/settings.py`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_BASE_URL` | unset | Chat model base URL; unset means not connected |
| `LLM_API_KEY` | unset | Bearer key; required when `LLM_BASE_URL` is set (startup exits 1 otherwise) |
| `LLM_MODEL` | empty | Model name; left out of the request when empty |
| `LLM_TIMEOUT_SECONDS` | 30 | Most one answer may take, counting the wait for a slot |
| `CHAT_INSTRUCTIONS_PATH` | unset | The instructions file used as the system prompt |
| `CHAT_ENABLED_FOR` | `admin` | Feature flag: `admin`, `all` (Admins and Viewers) or `none` |

The instructions file is not in this repository. The portal reads it at
question time from `CHAT_INSTRUCTIONS_PATH`. The panel shows "not connected"
when the model URL or the path is unset, or the file is missing, empty or
unreadable. A file that is not UTF-8 or is over 64 KB is caught when a
question is asked, and the answer says chat is not connected. No host or port
is built into the code.

### Where chat lives

A panel on the Home page, not a sixth section. The form posts to
`/chat/ask`. With HTMX the route returns one answer fragment that is added
below earlier answers; without JavaScript it returns the Home page with the
answer. Earlier answers live only in the page; nothing is stored. Roles the
flag does not allow get 404 from the route and no panel.

### The prompt

The system prompt is the instructions file followed by exactly two sections,
`## BALANCE TABLE` and `## SOURCES` (`app/chat/prompt.py`):

- `BALANCE TABLE` lists each account (name, category, entity name, balance to
  the cent, as-of date) and the totals for the latest day in
  `balances_daily` (summed from the accounts when that table has no rows).
  Totals are USD only.
- `SOURCES` holds up to 8 passages from search (`family-docs` and `tax-law`,
  `top_k` 8, `include_confidential` from the signed-in role). Each passage is
  labelled with its document title and page or page range, or its tax-law
  reference number and title, and fenced between `<<<PASSAGE n: label>>>`
  and `<<<END PASSAGE>>>` lines, with a note that passages are quoted
  material and never instructions.
- Passage text is cleaned first: leading `#` marks are removed from every
  line, any line that reads `BALANCE TABLE` or `SOURCES` once marks and
  punctuation are removed is dropped, and runs of three or more `<` or `>`
  are cut to one, so a passage cannot open a section or close its fence.
- Document, entity and account identifiers never go in the prompt.

The question is the user message.

### The model call

- Every request body is built server-side from fixed fields: `messages`,
  `stream: false`, `max_tokens` (700, or 500 with an image) and `model` when
  set. The route reads only `question` and one `image` from the form, so a
  client `chat_template_kwargs` never reaches the model.
- At most one image. It is decoded with Pillow (MIT-CMU license), turned
  upright, converted to RGB, shrunk so its long edge is at most 1024 px, and
  re-encoded as JPEG. Uploads over 10 MB or 40 megapixels are refused.
- A semaphore of 2 (the service's slot count) wraps every call. The wait for
  a slot counts toward `LLM_TIMEOUT_SECONDS`.
- Only `choices[0].message.content` is read; `reasoning_content` is ignored.
- One attempt. A timeout, an unreachable or refusing service, or an unusable
  body becomes a plain sentence. There is no retry loop.

### Rendering

- The answer is plain text. Markdown images are dropped, markdown links keep
  only their words, bare web addresses become "[link removed]", and Jinja
  autoescaping shows any HTML as text.
- The only links are the portal's own citations: a family document links to
  `/documents/<id>/preview#page=N`; a tax-law passage shows its reference
  number and title with no link.
- Balances are rendered from the table, never from model text: any account
  whose full name appears in the question or answer is shown with its figure
  and as-of date, and the overall total is shown when the question is about
  balances or totals. The label says the figures come from the portal and may
  have changed.
- Every answer carries "Educational, not legal or tax advice."

### Logs and privacy

Logs carry the outcome, whether an image was sent, the number of sources,
and search, model and total time in milliseconds (`chat_finished`). Never the
question, the prompt, the passages or the answer.

### Cross-site posts

The portal has no CSRF token. A post whose `Sec-Fetch-Site` header is
`cross-site` gets 403.

## Consequences

- Chat depends on two live services. When either is down, chat says so in one
  sentence and the rest of the portal is unaffected.
- The model may still state a figure in its text. The rendered balance box is
  the authoritative figure, and the instructions tell the model to copy
  figures from the table only.
- Fakes prove the request shape, the fencing, the rendering and the failure
  paths. They do not prove latency, that the model follows the instructions,
  or the real service's response shape.

Open assumptions:

- #ASSUME the portal is the only caller of the chat service, so a semaphore of
  2 matches its slots. #VERIFY with the model service's owner before
  enabling chat for Viewers.
- #ASSUME the service's default temperature is suitable; none is sent.
  #VERIFY against the service's benchmark settings before setting one.
- #ASSUME a search plus a model call fits 30 seconds with two users at once.
  #VERIFY by reading `elapsed_ms` from `chat_finished` logs in a test session
  against the live services.
- #EDGE browsers that send no Fetch Metadata headers are not protected from a
  cross-site post. #VERIFY that the household's tablets run a current browser.

## Related

- ADR-003: backend data aggregation (the cached-read rule this ADR excepts)
- ADR-005: authentication (the role that sets the flag and confidential access)
- ADR-008: document search (the search function chat calls)
