# GEMINI.md

Gemini CLI users: this project uses Claude Code as its primary AI development tool.
Most project context, conventions, and rules are written for Claude Code.

## Where to find project context

- **CLAUDE.md** (project root): full development rules, tech stack conventions,
  auth requirements, data layer rules, testing targets, and command reference.
- **AGENTS.md** (project root): agent types used in development.
- **docs/planning/tech-spec.md**: canonical stack, schema, endpoints, env vars.
- **docs/architecture/adr/**: architecture decision records.

## Tech stack summary

| Layer           | Technology                                  |
|-----------------|---------------------------------------------|
| Web framework   | FastAPI (Python 3.12)                       |
| Templates       | Jinja2 (server-rendered, no SPA)            |
| Interactivity   | HTMX (vendored static asset)                |
| CSS             | Tailwind CSS (compiled via CLI binary)      |
| Database        | SQLite via aiosqlite                        |
| Auth            | Cloudflare Zero Trust (JWT middleware only) |
| Scheduler       | APScheduler v3                              |
| Package manager | uv                                          |

## Critical rules

- **Authentication**: CF JWT middleware MUST validate the `aud` claim against
  `CF_ACCESS_APP_ID`. Skipping this allows tokens issued to other apps in the
  same Cloudflare tenant. This is `#CRITICAL` per RAD. See ADR-002.
- **Data layer**: route handlers read from SQLite only; they never call backend
  HTTP services directly. APScheduler refresh jobs are the only writers. See
  ADR-003.
- **Stale data**: a stale section must show the last cached value with a "last
  updated" label, never an error page or blank section.
- **Python 3.12 only**: do not introduce 3.13 syntax or features.
- **Vendored assets only**: HTMX and Chart.js v4 are static files in `static/`,
  never CDN-loaded.
- **Logging**: never log financial values, document contents, or full email
  addresses; structlog JSON format only.
