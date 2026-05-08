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
