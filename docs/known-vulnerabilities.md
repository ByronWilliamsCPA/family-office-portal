# Known Vulnerabilities

This file documents CVEs and dependency vulnerabilities that cannot be immediately
resolved. Each entry must be reviewed quarterly. No entry may age past 60 days
without reassessment. The OpenSSF release gate blocks releases for any vulnerability
older than 60 days regardless of reassessment status.

## Template

| Field | Value |
| --- | --- |
| CVE | CVE-YYYY-NNNNN |
| Package | package-name@version |
| Severity | CRITICAL / HIGH / MEDIUM / LOW |
| CVSS | 0.0 |
| Affected versions | \<constraint\> |
| Fixed in | version or "no fix available" |
| First detected | YYYY-MM-DD |
| Reassessment due | YYYY-MM-DD (60 days from first detected) |
| Mitigation | Description of compensating control or reason deferral is safe |
| Tracking | Link to upstream issue or PR |

## Active Entries

_None._ No accepted vulnerability is open; `[tool.pip-audit]` in `pyproject.toml`
carries an empty `ignore-vuln` list.

## Resolved Entries

| Field | Value |
| --- | --- |
| CVE | PYSEC-2022-42969 (aliases CVE-2022-42969, GHSA-w596-4wvx-j9j6) |
| Package | py@1.11.0 |
| Severity | MEDIUM |
| CVSS | 7.5 |
| Affected versions | py <= 1.11.0 |
| Fixed in | Not applicable: advisory withdrawn, no fix was released |
| First detected | 2026-05-07 |
| Reassessment due | 2026-07-06 (missed; closed at the 2026-11-03 reassessment) |
| Resolved | 2026-11-03 |
| Resolution | The upstream advisory (OSV record PYSEC-2022-42969) was withdrawn on 2026-06-09. A `pip-audit` run on 2026-11-03 against an export of `uv.lock` (all extras, no ignore flag, run outside the repository so no project configuration applied) reported no vulnerabilities, including for `py` 1.11.0. The OSV query for `py` 1.11.0 also returns none. The `ignore-vuln` entry in `pyproject.toml` and the `--ignore-vuln` flags in the two security workflows were removed. |
| Mitigation (historical) | `py` is a transitive dependency of `interrogate` (dev extra only; `uv tree --invert --package py` shows no other dependent, and pytest does not depend on it). It is not part of the production install. |
| Tracking | [pytest-dev/py#287](https://github.com/pytest-dev/py/issues/287) |

`py` 1.11.0 stays in `uv.lock` because the latest `interrogate` release (1.7.0) still
requires it. If a future advisory is filed against `py`, add a new Active entry.
