# ADR-002: Authentication via Cloudflare Zero Trust

> **Status**: Accepted
> **Date**: 2026-05-06

## TL;DR

We will use Cloudflare Zero Trust with email magic links for authentication, eliminating
all password management for primary users and ensuring 30-day device sessions so parents
are never unexpectedly logged out.

## Context

### Problem

The two primary users have very low technical proficiency and cannot reliably manage
passwords. Standard username/password authentication generates support calls whenever a
session expires or a password is forgotten. The portal must accept only the family's
two email addresses and provide persistent access on their personal devices.

### Constraints

- **Technical**: The portal is a private web application with a small, fixed user list;
  no public registration; no OAuth provider integration required
- **Business**: Any authentication friction creates a support call from a primary user;
  the login flow must be explained in a single sentence

### Significance

Authentication is the first thing primary users encounter. A wrong choice here -- one
that requires passwords, expires too frequently, or shows cryptic errors -- defeats the
portal's core purpose before the user sees any content.

## Decision

**We will use Cloudflare Zero Trust Access with one-time email links because it
requires no password management, integrates at the network layer (not the application
layer), and supports 30-day session cookies on trusted devices.**

### Rationale

Cloudflare Zero Trust sits in front of the application and validates sessions before
any request reaches the portal server. This means the portal itself has zero
authentication code to write, test, or maintain. The family email domain or specific
email addresses become the access policy. Magic links mean the login instruction to
a primary user is: "Check your email and tap the link."

## Options Considered

### Option 1: Cloudflare Zero Trust (Magic Link) ✓

**Pros**:

- No password to remember or reset
- Session handled by Cloudflare; portal server is auth-agnostic
- Policy enforced at network edge; reduces attack surface
- 30-day session cookies configurable per application
- One-time link expires after use; replay attacks not possible

**Cons**:

- Requires access to the family email account during login (acceptable -- users
  already have tablet email access)
- Cloudflare account dependency for a critical path

### Option 2: Username + Password (Application-Level)

**Pros**:

- No external dependency

**Cons**:

- Password resets generate support calls from low-proficiency users
- Portal must implement secure session management, CSRF protection, rate limiting
- Password storage requires hashing, rotation policy, breach response plan

### Option 3: Google / Apple SSO (OAuth)

**Pros**:

- Familiar flow for some users

**Cons**:

- Requires users to understand OAuth consent screens, which primary users
  cannot reliably navigate
- Google account coupling for a private family tool is unnecessary complexity
- Session duration limits depend on third-party provider decisions

## Consequences

### Positive

- Portal server receives only pre-authenticated requests; no auth middleware to maintain
- User-facing login instruction fits in one sentence
- Session duration is a Cloudflare configuration value, not a code change
- Two-level access (Viewer vs Admin) can be enforced via Cloudflare Access policies
  and/or a JWT claim check in the portal's middleware

### Trade-offs

- Cloudflare Access JWT validation must be implemented in the portal middleware to
  distinguish Viewer vs Admin roles; Cloudflare handles authentication but not
  role-based authorization at the application level
- Mitigation: Read Cloudflare Access JWT from `CF-Access-JWT-Assertion` header;
  validate against Cloudflare public keys; extract email claim; map to role

### Technical Debt

- If a third user (e.g., accountant) needs time-limited access, Cloudflare Access
  Service Tokens provide an API-key-style flow -- this requires no application changes,
  only a new Access policy

## Implementation

### Components Affected

1. **Portal middleware**: Validate `CF-Access-JWT-Assertion` header on every request;
   extract email; return 403 if JWT is absent or invalid (defense in depth -- Cloudflare
   should block unauthenticated requests before they reach the server)
2. **JWT audience validation** `#CRITICAL`: Verify the `aud` claim in the JWT against the
   Cloudflare Access Application ID (`CF_ACCESS_APP_ID` env var). Without this check,
   the middleware will accept valid tokens issued to any other application in the same
   Cloudflare tenant -- a critical security gap in multi-app Zero Trust deployments.
3. **Session display**: Show the authenticated user's email in the admin header so
   users know who is logged in
4. **Role mapping**: `family_email_1@domain.com` and `family_email_2@domain.com` →
   Viewer; admin emails → Admin; configured via environment variable list

### Testing Strategy

- Unit: JWT validation middleware with a fixture Cloudflare public key and test tokens
- Integration: Request with valid JWT → correct role; request without JWT → 403

## Security Considerations

### Trust model

Authentication is fully edge-terminated: Cloudflare Zero Trust Access issues and
verifies the magic-link session, then attaches a signed `CF-Access-JWT-Assertion`
header to every request forwarded to the portal's origin. The portal never sees a
password and never issues its own session cookie. This means the portal's entire
authentication trust boundary collapses to one question: does this JWT genuinely
come from Cloudflare Access for this application, and can it be trusted at
face value. Two properties must both hold for that trust to be sound:

1. **Signature validation**: the JWT must verify against Cloudflare's public keys
   fetched from `https://<CF_TEAM_DOMAIN>/cdn-cgi/access/certs` (cached with a TTL,
   not fetched per request). A JWT that fails signature verification must be
   rejected with 403, not logged and passed through.
2. **Audience (`aud`) claim validation** `#CRITICAL`: the JWT's `aud` claim must be
   checked against `CF_ACCESS_APP_ID`. Cloudflare Zero Trust tenants commonly host
   multiple Access-protected applications; every one of them can mint a
   validly-signed JWT for its own users. Skipping the `aud` check means any user
   with legitimate access to a *different* application in the same tenant can reuse
   their token against this portal. This is not a theoretical gap: it is the
   specific failure mode this ADR's Implementation section (`### Components
   Affected`, item 2) calls out, and it is the reason defense-in-depth middleware
   exists in the portal at all rather than trusting Cloudflare's edge block
   unconditionally.

Because the network edge is the sole enforcement point for identity, a
misconfigured Cloudflare Access policy (wrong email list, disabled application,
overly broad `aud` allowance) degrades directly into unauthorized access with no
application-level backstop other than the `aud` check above. This is an accepted
trade-off of Option 1 (see `## Options Considered`): it buys password-free access
for low-proficiency users at the cost of concentrating trust in Cloudflare's
policy configuration and the middleware's `aud` enforcement.

### Current implementation status

`app/middleware/cloudflare_access.py` is a **Phase 0 pass-through stub**: it
accepts every request unchanged and performs no signature or `aud` validation.
The stub is intentionally scoped to Phase 0 (scaffolding only; see `CLAUDE.md`
"Current phase"). It is tagged `#CRITICAL` / `#VERIFY` in the module docstring:
Phase 1 must replace `dispatch` with the fail-closed JWT validation pipeline
described above, and a fixture token carrying a foreign `aud` must be asserted to
return 403 in `tests/unit/test_middleware.py` before the stub is considered
removed. Until that lands, this repository has **no working authentication**
end-to-end in local/CI environments that bypass the Cloudflare edge (e.g. direct
requests to the origin, or test clients); production traffic is protected only
because Cloudflare Access itself sits in front of the deployed origin.

### Residual risks

- **Public key cache staleness**: if Cloudflare rotates its signing keys and the
  middleware's cached key set is not refreshed before the TTL allows, valid
  requests could be rejected (fail-safe) or, if the cache logic is inverted, stale
  keys could be trusted past their rotation (fail-unsafe). The TTL and refresh
  behavior must be tested explicitly in Phase 1, not assumed correct.
- **Direct-to-origin requests**: any network path that reaches the FastAPI origin
  without transiting Cloudflare Access (e.g., a misconfigured DNS/tunnel entry)
  bypasses the edge entirely. The `aud`-validated middleware is the only
  application-level control against this, which is why item 2 in `##
  Implementation` is marked `#CRITICAL` rather than left to Cloudflare alone.

## Validation

### Success Criteria

- [ ] Primary users can log in using only email, with no password or username
- [ ] Session persists for 30 days on trusted devices without re-authentication
- [ ] Expired session displays a plain-English prompt, not an error code
- [ ] Admin email addresses have access to admin-only routes; Viewer emails do not

### Review Schedule

- Initial: During Phase 0 Cloudflare setup
- Ongoing: If a primary user reports an unexpected logout

## Related

- [ADR-001](./adr-001-frontend-rendering-architecture.md): Server-side session
  validation is consistent with server-rendered architecture
- [Tech Spec](../../planning/tech-spec.md#6-security): JWT validation middleware details
- [Project Vision](../../planning/project-vision.md):
  Session behavior requirements (magic link, 30-day sessions)
