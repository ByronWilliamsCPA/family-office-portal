# Operations runbook

> **Status**: Active | **Audience**: the technical owner who runs the portal alone
>
> Evidence comes at three levels, and the text says which one applies where. (1)
> Checked against the portal code in this repository: most of this document.
> (2) Checked against the infrastructure repository's deployment definitions, by
> reading its files: the sidecar scheduling and the balance intake route, which
> are marked as such. (3) Not verified against a live deployment: everything in
> "Not yet proven" at the end. Settings are named, never valued. Placeholders such as `<portal-container>` and
> `<proxy-network-cidr>` stand for values that belong to your deployment.
> Anything that can only be confirmed on the real deployment is listed at the end
> under "Not yet proven".

## What runs where

| Piece | Where it runs | What it does |
| --- | --- | --- |
| Web process | One `uvicorn` process in the portal container | Serves every page, `/health`, the admin routes, the balance intake and the question panel |
| Scheduler | Inside the web process (APScheduler, started at app start-up) | Refreshes the SQLite cache and writes the daily balance snapshot |
| Document indexer | Separate command, `python -m app.retrieval.indexer` | Embeds chunk files and writes the `family-docs` collection in Qdrant |
| Tax-law collection | Separate command, `python -m app.retrieval.tax_law` | Embeds the tax-law knowledge-base file and writes the `tax-law` collection |

### Scheduler jobs

Names and intervals come from `app/scheduler.py`. Every job also runs once when the
app starts.

| Job | Runs | Fills |
| --- | --- | --- |
| `refresh_entities` | every 4 hours | `entities` |
| `refresh_holdings` | every 2 hours | `holdings` and `performance` |
| `refresh_positions` | every 2 hours | `positions` |
| `refresh_documents` | every 12 hours | `documents` |
| `snapshot_balances_daily` | daily at 12:00 in `DISPLAY_TIMEZONE` | `balances_daily` (copies today's stored balances) |

A job never runs twice at once; a run that is still going makes the next one skip.
The daily snapshot may start up to one hour late and still run. A backend with no URL
set is "not connected": its job logs one line and makes no outbound call.

Because every job runs at start-up, each restart makes one call to every connected
backend. A restart loop therefore calls the backends repeatedly.

### Why the two commands stay out of the web process

Bulk embedding uses the same CPU as the query embeddings behind search and would
compete with page requests. The web process never indexes. Run each command from
the portal image as its own short-lived process, off hours, one at a time. The
reasoning is in
[ADR-006](architecture/adr/adr-006-document-indexer.md) (indexer and tax-law
amendment) and [ADR-008](architecture/adr/adr-008-document-search.md) (search).

### One worker, and why

Run exactly one `uvicorn` worker. Four things are per process:

- The scheduler lives in the web process, so N workers would run every job N times.
- Every SQLite write goes through one in-process lock, so a second process would
  mean a second writer (see "Backups" and "Troubleshooting").
- The question panel allows two model calls at a time, counted per process, so N
  workers would allow 2N calls ([ADR-009](architecture/adr/adr-009-chat-live-model-call.md)).
- In-memory state in general is not shared between processes.

The `Dockerfile` already sets this. Its `CMD` is:

```text
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips=<value set in the Dockerfile>
```

### Proxy trust range

`--forwarded-allow-ips` makes uvicorn trust `X-Forwarded-*` headers only from the
reverse proxy. The image ships a broad private range as its default, and the
Dockerfile comment says to narrow it once the proxy network has a pinned subnet.
Override the container command so it names your proxy network, and never use `*`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 \
  --proxy-headers --forwarded-allow-ips="<proxy-network-cidr>"
```

The portal must be reachable only through the proxy with Authentik forward auth in
front of it ([ADR-005](architecture/adr/adr-005-authentication-authentik-forward-auth.md)).

## Start-up checks

`app/main.py` checks settings before the app starts serving. A failed check writes
one line to standard error and exits with status 1, so the container stops. Read it
with `docker logs <portal-container>`.

| Message begins | Cause | Names the variable |
| --- | --- | --- |
| `Missing required environment variable:` | One of `AUTHENTIK_JWT_SECRET`, `AUTHENTIK_ISSUER`, `AUTHENTIK_AUDIENCE`, `SQLITE_PATH` is unset or empty | Yes |
| `Invalid authentication configuration:` | `AUTHENTIK_JWT_SECRET` is shorter than 32 characters, still the stack placeholder, or has leading or trailing whitespace; or `FO_ADMIN_GROUP` and `FO_VIEWER_GROUP` are the same | Yes |
| `Invalid backend configuration:` | A backend URL is set without its key (all four `BACKEND_*_URL` and `BACKEND_*_API_KEY` pairs are checked); or `BALANCE_INTAKE_API_KEY` is set but too short, not printable ASCII, or equal to `AUTHENTIK_JWT_SECRET` | Yes |
| `Invalid chat configuration:` | `LLM_BASE_URL` is set without `LLM_API_KEY`, is not an http or https URL, the key is not ASCII, `LLM_TIMEOUT_SECONDS` is not a number above zero, `CHAT_RATE_LIMIT_PER_MINUTE` is above `CHAT_RATE_LIMIT_PER_HOUR`, or another chat setting does not parse (for example `CHAT_ENABLED_FOR` other than `admin`, `all` or `none`, or a rate limit that is not a whole number above zero) | Yes |

The messages never contain a value. A key with no URL is allowed and logged at info
level, because that backend is simply not connected.

These are not start-up checks, and a bad value does not stop the app:

- The search settings (`EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBEDDING_MODEL`,
  `EMBED_TIMEOUT_SECONDS`, `QDRANT_URL`, `QDRANT_API_KEY`, `CHUNKS_DIR`,
  `TAX_LAW_PATH`). The web process logs `document_search_misconfigured` with the
  reason and treats search as not connected. The indexer commands exit 2.
- `DISPLAY_TIMEZONE`. An unusable name logs `display_timezone_unusable` once and
  falls back to UTC, so check the log if "last updated" times look off by hours.
- `CHAT_INSTRUCTIONS_PATH`. An unreadable file logs `chat_instructions_unavailable`
  when someone asks a question, and the panel says it is not connected.

Every setting is listed by name, with its default, in `.env.example`. The app does
not read a `.env` file itself; supply the values through the container environment
(for example the compose `env_file` option). The README also describes each group
of settings.

## Backups

### What is durable and what is not

All of it lives in the one SQLite file at `SQLITE_PATH`.

| Table | Kind | If lost |
| --- | --- | --- |
| `balances_daily` | Durable history. The only table `app/db.py` marks as not a cache | Cannot be re-fetched. Past days are never rewritten |
| `account_balances` | Current balances | Refilled by the collector's next delivery, not by any backend pull; balance pages are empty or stale until then |
| `entities`, `holdings`, `performance`, `positions`, `documents` | Cache | Refilled by the refresh jobs from the backends |
| `refresh_log` | Diagnostic log of refresh outcomes | Lost history only; nothing depends on it |

The Qdrant collections are not in this file. They are rebuilt by running the two
commands again from the chunk files and the knowledge-base file.

### Take a backup

The database runs in WAL mode, so copying the `.db` file alone can miss recent
writes that sit in the `-wal` file. Use SQLite's own online backup, which reads a
consistent snapshot while the portal keeps running. This is a procedure for the
operator to run from the host that holds the data volume; write the backup to a
different disk or host afterwards.

```bash
sqlite3 "<data-dir>/<database-file>" ".backup '<backup-dir>/portal-$(date +%Y%m%d-%H%M).db'"
```

`VACUUM INTO` also produces a consistent copy (it fails if the target file exists):

```bash
sqlite3 "<data-dir>/<database-file>" "VACUUM INTO '<backup-dir>/portal-copy.db'"
```

The runtime image is distroless and has no `sqlite3` command. If the host has none
either, Python's built-in backup call works from inside the container (it writes to
a path the container user can write, such as the data volume):

```bash
docker exec <portal-container> python -c "import sqlite3; s=sqlite3.connect('<database-path-in-container>'); d=sqlite3.connect('<backup-path-in-container>'); s.backup(d); d.close(); s.close()"
```

### How often

Take one backup daily, shortly after the midday snapshot (for example 30 minutes
after 12:00 in `DISPLAY_TIMEZONE`), so each backup holds that day's row in
`balances_daily`. Keep several generations. Losing a day costs one day of history
that no source can supply again, so do not stretch the interval.

### Verify a backup

```bash
sqlite3 "<backup-file>" "PRAGMA integrity_check;"
sqlite3 "<backup-file>" "SELECT COUNT(*), MIN(date), MAX(date) FROM balances_daily;"
sqlite3 "<backup-file>" "PRAGMA user_version;"
```

Expect `ok` from the first, a row count and date range that match what you expect
(the count should only grow from backup to backup), and a `user_version` equal to
the highest migration number in `app/db.py` (2 at the time of writing). Test a
restore on a scratch copy now and then, so the first time you restore is not an
emergency.

### Restore

1. Stop the portal container, so nothing writes while you replace the file.
2. Move the old database aside together with its `-wal` and `-shm` files; do not
   delete them yet.
3. Copy the chosen backup to the path in `SQLITE_PATH`.
4. Make the file readable and writable by the container user (UID 65532 in the
   image), as the Dockerfile notes for the data volume.
5. Start the portal. Start-up applies any missing schema migrations and the
   scheduler refills the caches; the next balance delivery refills
   `account_balances`.
6. Check `/health`, open Finances, and compare the trend against the backup's
   `balances_daily` date range.

## Scheduling the indexer and tax-law commands

Both commands take no arguments and read their settings from the environment. Run
them off hours, never at the same moment, and never from the web process. One
shared lock file serves both, so even a slow run cannot overlap the next one.

| Command | Needs set | Exit 0 | Exit 1 | Exit 2 |
| --- | --- | --- | --- | --- |
| `python -m app.retrieval.indexer` | `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBEDDING_MODEL`, `QDRANT_URL`, `QDRANT_API_KEY`, `CHUNKS_DIR` | Every file handled | At least one file failed (unreadable, could not be embedded, or Qdrant refused that document) | Not configured or misconfigured, embedding service refused the key, or Qdrant stopped answering |
| `python -m app.retrieval.tax_law` | The same embedding and Qdrant settings, plus `TAX_LAW_PATH` | Collection matches the file | File unusable or embedding failed; the collection is unchanged. Also a file that would delete most stored points is refused | Not configured or misconfigured, key refused, or Qdrant stopped answering |

`CHUNKS_DIR` should be a read-only mount. With the settings unset the commands log
`indexer_not_connected` or `tax_law_not_connected` and exit 2.

### How the infrastructure repository runs them (2026-11-04)

The deployment definitions in the infrastructure repository do not use cron or a
systemd timer for these two commands. Verified by reading its portal stack
definition (not verified against a running system): a sidecar container started
from the same portal image runs a small loop. Once a day at the time in
`RETRIEVAL_JOBS_DAILY_AT` (24-hour `HH:MM`, UTC; empty means no scheduled run) it
runs the indexer and then the tax-law command, one at a time under a lock, and
each job is stopped after `RETRIEVAL_JOB_TIMEOUT_SECONDS`. A run can also be
started by creating a trigger file inside the sidecar container; the loop picks
the file up when it is idle or when the current cycle ends, so a manual run never
overlaps a scheduled one. The sidecar reports a failed cycle by turning unhealthy,
not by restarting, so watch its health status or its log.

Do not start the two commands by hand with `docker exec` while the sidecar is
running: that bypasses its lock. The cron and timer recipes below apply to a
deployment that does not use the sidecar. The portal-side commands and exit codes
above are the same either way.

### Cron

`flock -n -E 99` skips a run when another holds the lock and exits 99, which cannot
be confused with the commands' own codes. The container needs the settings, the
read-only mounts, and a network from which the embedding service and Qdrant resolve.

```bash
# Documents at 02:15 daily, tax law at 03:15 on Sundays (host local time).
15 2 * * * flock -n -E 99 /var/lock/portal-index.lock docker run --rm --env-file <env-file> --network <portal-network> -v <chunks-dir>:<chunks-dir-in-container>:ro <portal-image> python -m app.retrieval.indexer
15 3 * * 0 flock -n -E 99 /var/lock/portal-index.lock docker run --rm --env-file <env-file> --network <portal-network> -v <tax-law-dir>:<tax-law-dir-in-container>:ro <portal-image> python -m app.retrieval.tax_law
```

Set `CHUNKS_DIR` and `TAX_LAW_PATH` in the env file to the in-container paths. The
image has no entrypoint, so the command after the image name replaces its `CMD`.

### systemd timer (alternative)

```ini
# portal-indexer.service
[Unit]
Description=Family office portal document indexer

[Service]
Type=oneshot
ExecStart=/usr/bin/flock -n -E 99 /run/lock/portal-index.lock /usr/bin/docker run --rm --env-file <env-file> --network <portal-network> -v <chunks-dir>:<chunks-dir-in-container>:ro <portal-image> python -m app.retrieval.indexer
```

```ini
# portal-indexer.timer
[Unit]
Description=Run the portal document indexer nightly

[Timer]
OnCalendar=*-*-* 02:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

Make the tax-law pair the same way with its own `OnCalendar` line and the same lock
path.

### After a run

- Check the exit code (`systemctl status portal-indexer.service`, or the cron mail
  or log). Exit 99 means the run was skipped because another held the lock.
- Indexer log: `index_run_finished` carries a count per outcome. These events mean
  work to do: `document_write_failed`, `chunk_set_unreadable`,
  `document_embedding_failed`, `indexer_misconfigured`, `index_run_stopped`.
- `orphan_check_skipped_empty_directory` or `orphan_check_skipped_too_many_missing`
  means the clean-up of removed documents was skipped on purpose, usually because
  the chunk mount was empty or not ready. Fix the mount before trusting the index.
- Tax-law log: `tax_law_index_finished` (with `indexed`, `removed` and
  `skipped_blank` counts) is success; `tax_law_index_failed` and
  `tax_law_index_stopped` are not. The logs never contain document or knowledge-base
  text.
- Sign in as an Admin and ask the question panel something a known document
  answers, to see the new content come back with its source.

## Health and staleness

### The health check

`GET /health` needs no sign-in and returns HTTP 200 with
`{"status": "ok", "service": "family-office-portal"}`. It is a fixed answer: it does
not look at the database or any backend, so it proves only that the web process is
up. The image's `HEALTHCHECK` calls it every 30 seconds. To see whether data is
fresh, use the refresh status below.

### What people see

Each section shows "Last updated" and the time in `DISPLAY_TIMEZONE`, then "This may
be out of date." when the data is older than its threshold. A section never goes
blank or shows an error because a backend failed: a failed refresh leaves the old rows
and the old label. Thresholds come from `app/cache.py`:

| Section | Stale after | Notes |
| --- | --- | --- |
| Entities | 8 hours | Refreshed every 4 hours |
| Holdings and performance | 4 hours | Refreshed every 2 hours; pp-security-master is alpha, so occasional failures are expected |
| Positions | 4 hours | Refreshed every 2 hours |
| Documents | 24 hours | Refreshed every 12 hours |
| Balances | 24 hours | Judged per provider: the section is as fresh as the provider whose last delivery is oldest |

Two other labels are not staleness. "Not connected yet" means that backend has no URL
set. "Not updated yet" means the backend is connected but no refresh has succeeded.

### The admin endpoints

Both need an Admin identity, so call them the way you normally reach the portal
through Authentik (a signed-in browser session, or any client that completes the
forward auth). They return JSON.

`GET /admin/refresh-status` lists, per service that has run, `last_success_at`,
`last_error_at`, `last_error_message` and `is_stale`. Service names are
`llc-manager`, `pp-security-master`, `xero_crypto`, `llc-manager-documents`,
`balances` and `balances-daily`. Error messages are the exception class and a short
text, never a payload.

`POST /admin/refresh/{service}` queues one refresh and returns 202 immediately.
`{service}` is one of `entities`, `holdings`, `positions` or `documents`. Send a JSON
body, `{}` is enough:

```bash
curl -X POST "https://<portal-host>/admin/refresh/entities" -H "Content-Type: application/json" -d '{}'
```

Balances cannot be triggered, because the collector delivers them. After a trigger,
read `/admin/refresh-status` again to see the result.

## Balance intake

A collector delivers account balances to `POST /api/v1/balances`. Apart from
`/health` and `/static/` (public, per ADR-005), it is the only route that is not
behind a signed-in identity; the proxy's forward auth must not sit in front of it, and no public route should forward to it. In the infrastructure
repository's definitions (verified by reading them, not against a running system)
the collector reaches the portal through a separate internal Traefik route that
requires a client certificate and accepts only `POST` on that exact path, not
directly on a Docker network; see the 2026-11-04 amendment in ADR-005.

- **Authentication**: the header `X-API-Key` must equal `BALANCE_INTAKE_API_KEY`,
  compared in constant time before the body is read. The setting must be at least 32
  printable ASCII characters and differ from `AUTHENTIK_JWT_SECRET`.
- **Answers**: 200 with a receipt when stored; 401 for a missing or wrong key; 404
  when `BALANCE_INTAKE_API_KEY` is unset or blank, because the endpoint is then
  switched off; 413 above 6 MiB; 422 when the body fails validation (nothing is
  stored, and the error names fields, never values); 503 when the database write
  failed (nothing changed).
- **Body**: `{"items": [...], "total": n}`. The exact fields are in
  `docs/api/openapi.json`.
- **What a delivery replaces**: per provider. A provider is the text before the first
  colon in an account id. Every provider present in the delivery has its stored rows
  replaced; a provider not in the delivery keeps its last rows. The whole delivery is
  stored in one transaction, or not at all.
- **A provider that stops reporting** keeps its last values in the totals and the
  trend, and the balances section is labelled out of date after 24 hours. There is
  no way to retire a provider yet.
- **Daily history**: every delivery also writes today's row per account into
  `balances_daily`, and the `snapshot_balances_daily` job copies the stored balances
  once at start-up and then daily at 12:00 in `DISPLAY_TIMEZONE`. Earlier days are
  never deleted.
- **Checking a delivery landed**: read `/admin/refresh-status` and look at the
  `balances` entry, or open Finances and read the "as of" dates.
- **Setting names differ**: the portal calls the intake key `BALANCE_INTAKE_API_KEY`;
  the collector stack in the infrastructure repository calls the same value
  `PORTAL_ADMIN_API_KEY`. The two must hold the same value (verified by name in the
  stack definition; the values are never written down here).

In your own commands and notes, write the key as `<intake-key>`. To rotate it, see
"Secrets".

## The question panel and search

The panel on Home answers questions about documents and balances. Each question makes
two live calls while the request is open: a document search, then the chat model
([ADR-009](architecture/adr/adr-009-chat-live-model-call.md)). It works only when all
of this is in place:

1. The chat model: `LLM_BASE_URL` and `LLM_API_KEY` set (and optionally `LLM_MODEL`,
   `LLM_TIMEOUT_SECONDS`).
2. An instructions file at `CHAT_INSTRUCTIONS_PATH`, readable by the container. It is
   read at question time and is kept outside this repository.
3. Document search: an embedding service (`EMBED_BASE_URL`, `EMBED_API_KEY`,
   `EMBEDDING_MODEL`) and a vector store (`QDRANT_URL`, `QDRANT_API_KEY`), both valid.
4. Content in the index, from the indexer commands above.
5. `CHAT_ENABLED_FOR`: `admin` (default) shows the panel to Admins only, `all` to
   Admins and Viewers, `none` to nobody. A person outside the audience sees no panel,
   and the route answers 404 for them.

Nothing is stored: no questions, no answers. Logs carry timings and counts only.

### Per-user rate limits

Each signed-in person is limited in the portal itself, in memory, keyed by the
signed-in username (Admins and Viewers alike; it is never logged or shown). Two
settings, both optional, whole numbers above zero:

| Setting | Default | Meaning |
| --- | --- | --- |
| `CHAT_RATE_LIMIT_PER_MINUTE` | 6 | Most questions one person may ask in any 60 seconds |
| `CHAT_RATE_LIMIT_PER_HOUR` | 60 | Most questions one person may ask in any hour |

The minute value must not be above the hour value; otherwise start-up exits 1 with
`Invalid chat configuration:` and a message naming both variables. On top of the
quota, one person can have only one question in flight at a time, always, so nobody
can hold both model slots.

What a person sees: HTTP 429 with a `Retry-After` header and a plain sentence in the
panel, never a number or a name:

- A question already running: "Your last question is still being answered. Please
  wait for it to finish."
- Over the minute limit: "You have asked several questions very quickly. Please wait
  a minute and try again."
- Over the hour limit: a sentence giving the wait in minutes.

What counts:

- A refused request is not recorded, so retrying while limited never lengthens the
  wait.
- A question refused before any work is done (blank, too long, a bad picture, a body
  over the size cap, chat not connected) is refunded and does not count.
- Anything that reaches the search or the model counts, even if that call then fails.
  A question cut off by a disconnect also stays counted.
- A question not finished after five minutes is treated as abandoned, so a hung
  upload cannot lock a person out.

Limits of the design ([ADR-009](architecture/adr/adr-009-chat-live-model-call.md)):

- The counters are in memory, so a restart resets every quota. The model timeout and
  the two-call cap still bound the load across a restart.
- The limiter assumes one worker process and one event loop, as the single-worker rule
  above already requires. With N workers a person could ask N times as many questions.
- A person on two tablets shares one budget; two people never share one.
- There is no per-address limit and no shared budget across people. The windows are
  fixed at 60 seconds and one hour, and there is no per-role setting.
- Any 429 under the panel, including one from the proxy, is shown as page content, so
  the proxy must not return a 429 body an outsider controls.

With these limits in place, setting `CHAT_ENABLED_FOR` to `all` is reasonable to
consider, but it still needs every prerequisite above (model, instructions file,
search and an index). Whether to open the panel to Viewers is the owner's call.

### What a model outage looks like

The person sees a plain sentence in the panel and the rest of the portal is
unaffected:

- Model down or unreachable: "The question service is not available right now.
  Please try later."
- Model too slow: "The answer took too long. Please try again in a minute."
- Search down: "Document search is not responding right now. Please try later."
- Not configured: "Questions are not connected yet. Please try again later."

## Secrets

Secrets are documented here by name only. Where each one lives:

| Name | Held by | Used for |
| --- | --- | --- |
| `AUTHENTIK_JWT_SECRET` | Authentik (the provider's client secret) and the portal's container environment | Signs and verifies the identity token on every request |
| `BALANCE_INTAKE_API_KEY` | The portal's container environment and the collector | Authenticates balance deliveries |
| `BACKEND_LLC_MANAGER_API_KEY`, `BACKEND_PP_SECURITY_API_KEY`, `BACKEND_XERO_CRYPTO_API_KEY`, `BACKEND_DATA_INGESTOR_API_KEY` | The portal's container environment and each backend | Sent to that backend as `X-API-Key` |
| `LLM_API_KEY`, `EMBED_API_KEY`, `QDRANT_API_KEY` | The environment of the web process and of the two commands, plus each service | Bearer keys for the model, embedding service and vector store |

Supply every value from your deployment's secret store into the container
environment. Never commit one, paste one into an issue or a chat, or leave one in
shell history. The chat instructions file and the tax-law file are sensitive content
rather than keys; keep them outside the repository and readable only as needed.

### Rotating `AUTHENTIK_JWT_SECRET`

The secret both signs and verifies, and there is no overlap and no hot reload, so
rotation has a gap in which every request gets 403. Do all of it in one maintenance
window:

1. Change the provider's client secret in Authentik. Authentik signs with the new
   value at once.
2. Put the new value into the portal's environment and restart the container. The
   portal reads it only at start-up.
3. Open the portal and confirm a signed-in page loads.

If you recreate the provider rather than rotate its secret, it also gets a new client
ID, so update `AUTHENTIK_AUDIENCE` in the same step. Keep the provider
single-application: because the secret is shared with the provider, no other
application may use it. Details are in
[ADR-005](architecture/adr/adr-005-authentication-authentik-forward-auth.md) and
`SECURITY.md`.

### Rotating `BALANCE_INTAKE_API_KEY`

The portal accepts one key at a time. Set the new value in the portal's environment
and restart it, then give the collector the same value. Until the collector is
updated its deliveries get 401 and change nothing; stored balances stay in place.
Confirm one delivery succeeds afterwards.

### Key hygiene

- Every key must be unique to its purpose; the intake key must differ from
  `AUTHENTIK_JWT_SECRET`, and start-up enforces that.
- Rotate on the cadence in `SECURITY.md`, and at once if exposure is suspected.
- The portal logs no key, token, balance, document text or question text; do not
  add any to logs, tickets or notes.

## Troubleshooting

| Symptom | Likely cause | Check | Fix |
| --- | --- | --- | --- |
| Container exits at start | A start-up check failed | `docker logs <portal-container>` for the `Missing` or `Invalid ... configuration` line, which names the variable | Correct that variable and restart; see "Start-up checks" |
| A section is empty | Backend not connected, or connected but never refreshed | The page says "Not connected yet" or "Not updated yet"; `/admin/refresh-status` | Set the backend's URL and key pair, or fix the backend and trigger a refresh |
| "Not connected yet" tiles | The backend URL is unset (or, for Finances balances, nothing has been delivered) | `.env.example` entry for that backend; for balances, the `balances` entry in refresh status | Set the URL and key; for balances, check the collector |
| "This may be out of date" labels | A refresh has failed for longer than the threshold | `last_error_message` in `/admin/refresh-status`; container log lines `refresh_failed`, `refresh_write_failed` | Fix the backend or the key, then `POST /admin/refresh/{service}`. Holdings failures from pp-security-master are expected now and then |
| 403 for everyone | Secret mismatch after a rotation, new provider ID not applied, proxy not forwarding `X-authentik-jwt`, clock skew above 10 seconds, or group names not matching `FO_ADMIN_GROUP` and `FO_VIEWER_GROUP` | The middleware log line names a reason category, never the token | Finish the rotation steps, align `AUTHENTIK_AUDIENCE` and `AUTHENTIK_ISSUER`, check the proxy contract in ADR-005 and the host clock |
| 403 for one person | Not in either group | Their group membership in Authentik | Add them to the right group; a removed user can keep a role for up to 24 hours |
| 404 on `/api/v1/balances` | `BALANCE_INTAKE_API_KEY` is unset or blank | Is the setting present in the container environment | Set it (32 or more printable ASCII characters) and restart. A 401 instead means the key is wrong |
| Question panel says it is not connected | Model settings, instructions file or search missing | Container log: `chat_instructions_unavailable`, `document_search_misconfigured`; the sentence shown names which part | Complete the list under "The question panel and search" |
| Panel not shown to a Viewer | `CHAT_ENABLED_FOR` is `admin` or `none` | The setting | Set `all` when you decide to open it; see "Per-user rate limits" |
| A person is told to wait | The per-user limit or the one-question-at-a-time rule | Log event `chat_rate_limited`, which carries the reason (`in_flight`, `per_minute` or `per_hour`) and the retry seconds only; the response has a `Retry-After` header | Wait and ask again; if the limit is too tight for the household, raise `CHAT_RATE_LIMIT_PER_MINUTE` or `CHAT_RATE_LIMIT_PER_HOUR` and restart |
| First page after a restart is slow or old | Every job runs at start-up, and backends are called with a timeout of `BACKEND_TIMEOUT_SECONDS` (default 10) | `/admin/refresh-status` after a minute; this cause is unconfirmed on the real deployment | Wait for the start-up refresh to finish; if it repeats, look for a restart loop |
| SQLite "database is locked" | A second writer: another worker, another container on the same file, or an open manual `sqlite3` session holding a write | Worker count is 1; `docker ps` for a second container using the volume; close manual sessions | Run one worker and one container per database file. Writes wait up to 5 seconds before giving up |

## Not yet proven

These can be confirmed only on the real deployment:

- The real forward-auth header: that the proxy sends `X-authentik-jwt` with the claims
  the portal expects, and replaces any client-supplied copy.
- Document preview (PDF) on a tablet.
- That the Finances totals match the source system to the cent.
- The collector's real payload against the intake's validation.
- That the backends accept one call per restart and honour their key checks.
- That no public route forwards `/api/v1/balances`, and that the internal intake route
  refuses a caller without a client certificate (ADR-005, 2026-11-04 amendment).
- That the sidecar loop runs the indexer and tax-law commands at the configured time.
- The narrowed `--forwarded-allow-ips` value working with the pinned proxy network.
