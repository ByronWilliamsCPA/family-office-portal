# Known Vulnerabilities Template

> Copy this template into `docs/known-vulnerabilities.md` to add a new entry.
> Required by global CLAUDE.md whenever `pip-audit` reports a vulnerability that
> cannot be immediately resolved.

## Process

- File a new entry in `docs/known-vulnerabilities.md` for every unresolved
  pip-audit finding.
- Reassess every entry within 60 days of its `First detected` date.
- The OpenSSF release gate blocks any release that includes a vulnerability
  older than 60 days, regardless of reassessment status.
- Remove an entry only when the underlying CVE is resolved (dependency upgraded
  or patched). Move resolved entries to the Resolved Entries section.

## Entry Template

| Field | Value |
| --- | --- |
| CVE | `CVE-YYYY-NNNN` or `GHSA-xxxx-yyyy-zzzz` |
| Package | `package-name@version` |
| Severity | CRITICAL / HIGH / MEDIUM / LOW |
| CVSS | `0.0` |
| Affected versions | `<constraint>` |
| Fixed in | version or "no fix available" |
| First detected | `YYYY-MM-DD` |
| Reassessment due | `YYYY-MM-DD` (60 days from first detected) |
| Mitigation | Compensating control or reason deferral is safe |
| Tracking | Link to upstream issue or PR |
