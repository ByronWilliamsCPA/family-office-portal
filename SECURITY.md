# Security Policy

## Scope

This repository contains the Family Office Estate Portal, a private read-only
web application that aggregates financial and entity data from internal backend
services. It handles non-public financial information and operates behind
Cloudflare Zero Trust access controls.

## Supported Versions

Only the current `main` branch is actively supported.

| Version | Supported |
| --- | --- |
| `main` | Yes |
| All others | No |

## Reporting a Vulnerability

Do not open a public GitHub issue for security vulnerabilities.

**Use GitHub Private Vulnerability Reporting (PVR):**
[https://github.com/ByronWilliamsCPA/family-office-portal/security/advisories/new](https://github.com/ByronWilliamsCPA/family-office-portal/security/advisories/new)

Include in your report:

- A description of the vulnerability
- Steps to reproduce or a proof-of-concept
- The potential impact (confidentiality, integrity, availability)
- Any suggested mitigations

You will receive an acknowledgment within 72 hours and a resolution update
within 14 days.

## Security Architecture

Authentication is handled entirely by Cloudflare Zero Trust at the network
edge. The application validates Cloudflare Access JWTs on every non-static
request and maps the `email` claim to `Viewer` or `Admin` role. No
password-based auth, OAuth flows, or session cookies are implemented.

The application is read-only: it never writes to or directly contacts upstream
commercial systems. All backend data flows through an internal SQLite cache
populated by scheduled refresh jobs. The cache database is never exposed to
the network.

## Known Limitations

- The `pp-security-master` backend is alpha-status; its API contract is
  unstable. Stale data from this backend is surfaced with a timestamp
  rather than shown as an error.
- Content Security Policy headers beyond FastAPI defaults are not yet
  implemented. Header hardening is planned before production deployment.

## Dependency Scanning

Dependencies are scanned with `uv run pip-audit` before each release.
Known unfixed CVEs are documented in `docs/known-vulnerabilities.md` and
reviewed quarterly. No vulnerability older than 60 days may be left without
reassessment.
