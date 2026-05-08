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

| Field | Value |
| --- | --- |
| CVE | PYSEC-2022-42969 |
| Package | py@1.11.0 |
| Severity | MEDIUM |
| CVSS | 7.5 |
| Affected versions | py <= 1.11.0 |
| Fixed in | No fix available (abandoned package) |
| First detected | 2026-05-07 |
| Reassessment due | 2026-07-06 |
| Mitigation | Transitive dependency via `interrogate` (dev-only). `py` is used only during test collection by pytest and is never reachable from production code or network input. The ReDoS vector requires attacker-controlled input to `py.path` string functions, which are not called in this project. Risk confined to developer machines running pre-commit or pytest. |
| Tracking | [pytest-dev/py#287](https://github.com/pytest-dev/py/issues/287) |

## Resolved Entries

_None yet._
