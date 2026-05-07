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

_No known vulnerabilities at this time. Run `uv run pip-audit` to check._

## Resolved Entries

_None yet._
