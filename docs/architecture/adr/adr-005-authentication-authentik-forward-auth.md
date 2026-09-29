# ADR-005: Authentication via Authentik Forward Auth

> **Status**: Accepted
> **Date**: 2026-09-29
> **Supersedes**: [ADR-002](adr-002-authentication-cloudflare-zero-trust.md)

## TL;DR

Authentik authenticates every request before it reaches the portal, through a
Traefik forward-auth middleware in front of the portal. The portal validates
the signed `X-authentik-jwt` header on every non-public request and maps the
token's Authentik groups to its two roles. The portal holds no passwords,
sessions, cookies, or OAuth flows. Everything on the Authentik and Traefik side
is owned by the homelab-infra repository; this ADR records only the contract
the portal relies on.

## Context

### Problem

ADR-002 put Cloudflare Zero Trust Access in front of the portal. The owner has
since confirmed an organization-wide move to self-hosted Authentik for identity
across the family office's applications. The portal must follow that move
without losing the property ADR-002 was chosen for: the two primary users never
manage a password and are not logged out unexpectedly.

In the homelab pattern, Traefik forwards each request to the Authentik embedded
outpost. On success the outpost returns identity headers to Traefik, which
passes them to the application: plain `X-authentik-username`,
`X-authentik-groups`, `X-authentik-email` and `X-authentik-name` headers, plus
a signed `X-authentik-jwt` header carrying the same identity as JWT claims.

### Constraints

- **Technical**: the portal container shares a Traefik network with other
  containers, and any of them can send arbitrary headers straight to the
  portal without passing through Authentik
- **Technical**: the portal stays a read-only, server-rendered app (ADR-001);
  adding login, callback, and logout pages is out of proportion to its job
- **Business**: carried over from ADR-002. Primary users never type or reset a
  password, device sessions last long enough that a primary user is not asked
  to sign in again during normal use, and the sign-in step can be explained in
  one sentence
- **Ownership**: the Authentik provider, application, groups, Traefik routers
  and middleware chain, session length, and passkey enrollment flows live in
  homelab-infra. The portal repository must not define or duplicate them

### Significance

The portal's whole authentication trust boundary is one question: did this
request's identity come from Authentik for this application? Getting it wrong
either locks the primary users out or lets any container on the shared network
impersonate an Admin.

## Decision

**We will accept identity only from the signed `X-authentik-jwt` header,
verify it against the Authentik provider's published keys, and map its
`groups` claim to the portal's Viewer and Admin roles.**

1. Traefik routes the portal's hostname through a forward-auth middleware
   pointing at the Authentik embedded outpost. Both are defined in
   homelab-infra.
2. The portal validates `X-authentik-jwt` on every request except `/health`
   and `/static/`:
   - signature: RS256 only, against the key whose `kid` matches, taken from
     the provider JWKS at `AUTHENTIK_JWKS_URL` (which must be `https://`)
   - `iss` must equal `AUTHENTIK_ISSUER` exactly
   - `aud` must equal or contain `AUTHENTIK_AUDIENCE`
   - `exp`, `iss` and `aud` are required; `exp` must be in the future, and
     `nbf` and `iat`, when present, must not be; 10 s leeway for clock skew
     (`JWT_LEEWAY_SECONDS`), because the token's `iat` and `exp` follow the
     Authentik session's access-token validity rather than the request
   - a non-empty identity claim (`preferred_username`, falling back to `sub`)
     is required
3. The plain `X-authentik-username`, `X-authentik-groups`, `X-authentik-email`
   and `X-authentik-name` headers are never read. They are unsigned, so any
   container on the shared network could forge them.
4. Roles come from the `groups` claim with exact, case-sensitive matching:
   `fo-admin` grants Admin and `fo-viewer` grants Viewer (names configurable
   through `FO_ADMIN_GROUP` and `FO_VIEWER_GROUP`). Admin wins when a user is
   in both. Anyone else gets 403, including members of broader homelab groups.
5. `/admin` and `/admin/...` paths require Admin (`/adminX` is an ordinary
   protected path). Public and admin checks use the routed path, with any
   ASGI `root_path` prefix stripped the way Starlette's router does, so
   mounting the app under a prefix cannot move an admin route past the role
   check. Every failure is a 403 with a plain "Access denied" body: missing
   header, malformed or invalid token, JWKS unreachable, no portal group, or a
   Viewer on an admin path.
6. JWKS keys are fetched lazily, cached for `AUTHENTIK_JWKS_CACHE_SECONDS`
   (default 600), and refetched early when a token names an unknown `kid`.
   Fetch attempts, failed ones included, are limited to one per 30 seconds
   (or per TTL, if shorter). When a refetch fails, the last good key set keeps
   validating tokens for one extra TTL, then every request fails closed. The
   fetch ignores proxy settings from the environment, follows no redirects,
   caps the body at 64 KiB and must finish within 10 seconds.

### Rationale

Forward auth keeps authentication entirely outside the application, as
ADR-002 did, so the portal still has no login code to write or maintain. The
signed JWT is the only header that proves Authentik produced it; validating it
turns the shared network from a trust assumption into a non-issue.

### Contract with homelab-infra

The portal works only if homelab-infra provides the following. Each item is an
input to the portal's configuration or a behaviour the portal assumes; none of
it is configured from this repository.

| # | Contract item | Portal setting or check |
| --- | --- | --- |
| 1 | Every request for the portal's hostname passes Traefik forward auth to the Authentik outpost | Portal returns 403 to anything without a valid token |
| 2 | Traefik forwards the outpost's `X-authentik-jwt` response header to the portal, replacing any client-supplied value | Header name `X-authentik-jwt` |
| 3 | The proxy provider signs with an RSA key (RS256) and sets `kid`. A provider with no signing key falls back to HS256 with the client secret, which the portal rejects | `algorithms=["RS256"]` |
| 4 | The provider JWKS is served over https and reachable from the portal container | `AUTHENTIK_JWKS_URL`, for example `https://<authentik-host>/application/o/<slug>/jwks/` |
| 5 | The token issuer string, including any trailing slash | `AUTHENTIK_ISSUER`, for example `https://<authentik-host>/application/o/<slug>/` |
| 6 | The token audience is the provider's client ID | `AUTHENTIK_AUDIENCE` |
| 7 | Groups `fo-admin` and `fo-viewer` exist, only the right people are members, and the token's `groups` claim lists group names | `FO_ADMIN_GROUP`, `FO_VIEWER_GROUP` |
| 8 | The token carries `preferred_username` (or at least `sub`) | Identity claim check |
| 9 | Authentik and the portal host keep NTP-synced clocks, within 10 s of each other | 10 s leeway for clock skew on `exp`, `nbf`, `iat` (`#ASSUME`; `#VERIFY` by comparing `date -u +%s.%N` or `chronyc tracking` on both hosts) |
| 10 | Primary users sign in without a password (passkeys, for example Face ID on their tablets) | Carried over from ADR-002; not enforced by the portal |
| 11 | Authentik device sessions are long, so a primary user is not asked to sign in again during normal use, and an expired session shows Authentik's plain-English sign-in page rather than an error | Carried over from ADR-002; not enforced by the portal |
| 12 | The portal is reachable only through Traefik (no published host port) | Deployment |
| 13 | The portal has its own single-application Authentik proxy provider, not a domain-level forward-auth provider shared with sibling applications | `AUTHENTIK_AUDIENCE` and `AUTHENTIK_ISSUER` unique to the portal |

Contract item 13 is a requirement on homelab-infra, not an infrastructure
design. A provider shared across applications issues every application behind
the same forward-auth chain tokens with the same `iss` and `aud`, so the
portal's issuer and audience checks could not tell them apart: a compromised
sibling application could replay an Admin user's token to the portal and be
accepted. `#VERIFY`: decode a real `X-authentik-jwt` from the deployed outpost
and confirm its `aud` (the provider's client ID) and `iss` belong to a
provider bound only to the portal, and that no other application's tokens carry
the same `aud`.

Once the portal has a Dockerfile, it must run uvicorn with `--proxy-headers`
and `--forwarded-allow-ips` restricted to the Traefik network's address range,
so forwarded client addresses are trusted only from Traefik.

## Options Considered

### Option 1: Forward auth, validate the signed JWT ✓

**Pros**:

- No login, callback, logout, or session code in the portal
- Identity cannot be forged by other containers on the shared network
- Access changes are Authentik group membership changes, not portal redeploys
- Matches how the other homelab applications are published

**Cons**:

- The portal depends on Authentik's JWKS endpoint being reachable; the portal
  fails closed while it is not
- Claim names and signing algorithm are Authentik configuration the portal
  cannot see, so they must be verified against a real token

### Option 2: Forward auth, trust the plain identity headers

**Pros**:

- Simplest possible middleware: read a header, map a group

**Cons**:

- Any container on the shared Traefik network can send
  `X-authentik-groups: fo-admin` and become an Admin. Rejected.

### Option 3: Portal as an OIDC client of Authentik

**Pros**:

- Standard OIDC flow with no dependency on forward-auth headers

**Cons**:

- Adds login, callback, logout, and session cookie handling to a read-only app,
  which ADR-002 and `CLAUDE.md` rule out
- Duplicates what forward auth already does at the edge

### Option 4: Stay on Cloudflare Zero Trust (ADR-002)

**Pros**:

- Already designed; no rework

**Cons**:

- Conflicts with the owner-confirmed organization-wide move to Authentik
- Keeps a separate identity system for one application

## Consequences

### Positive

- The portal no longer needs `CF_TEAM_DOMAIN`, `CF_ACCESS_APP_ID`,
  `VIEWER_EMAILS`, or `ADMIN_EMAILS`. Access is managed in Authentik groups.
- Identity forgery from the shared network is closed by design, not by
  network policy
- The login experience for primary users is owned by one system shared with the
  rest of the homelab

### Trade-offs

- Portal availability now depends on self-hosted Traefik and Authentik; their
  backup and restore procedures belong to homelab-infra and the family office
  continuity plan
- Mitigation: the portal fails closed with a plain "Access denied" page rather
  than an error trace, and `/health` stays public so monitoring can tell a
  portal outage from an identity outage

### Technical Debt

- A later accountant or advisor login needs only an Authentik account and group
  membership; if a third role is ever needed, `role_from_groups` gains a group
  and the role check gains a path rule
- `structlog` has no JSON configuration yet; auth denials are logged with the
  default renderer until the logging setup lands

## Implementation

### Components Affected

1. **`app/middleware/authentik.py`**: pure ASGI middleware
   (`AuthentikAuthMiddleware`), JWKS fetch and cache (`JwksCache`), token
   validation, role mapping, and the `Principal` stored on
   `request.state.principal`. Replaces the Phase 0 pass-through stub
   `app/middleware/cloudflare_access.py`, which is deleted.
2. **`app/main.py`**: required env vars now include `AUTHENTIK_JWKS_URL`,
   `AUTHENTIK_ISSUER` and `AUTHENTIK_AUDIENCE`; startup exits with status 1 and
   a message naming the variable when one is missing or when the JWKS URL is
   not `https://`.
3. **Optional settings**, each with a documented default: `FO_ADMIN_GROUP`
   (`fo-admin`), `FO_VIEWER_GROUP` (`fo-viewer`), and
   `AUTHENTIK_JWKS_CACHE_SECONDS` (`600`).
4. **Postman collection and Newman workflow**: protected routes send
   `X-authentik-jwt: {{authentikJwt}}`; with no token (the CI default) they
   must answer 403.

### Testing Strategy

- Unit (`tests/unit/test_middleware.py`): valid Viewer and Admin tokens, `aud`
  as a list, and every rejection path (missing, garbage, expired, `exp` equal
  to now, missing claims, wrong `aud` or `iss`, wrong key, tampered signature
  or payload, `alg=none`, HS256 keyed with the public key, future `nbf` or
  `iat`, missing identity, no portal group), plain headers with and without a
  JWT, JWKS failure, cache reuse, TTL expiry, rate-limited refetch on unknown
  `kid`, public and admin paths, and startup exits
- Integration: the real `app.main` app with an Admin token for route tests, and
  anonymous and Viewer requests against `/admin` routes

## Security Considerations

### Trust model

Authentication happens in Authentik; the portal decides only whether the
forwarded identity is genuine and which role it grants. Three properties must
hold together:

1. **Only the signed header counts** `#CRITICAL`: the plain identity headers
   are forgeable by any neighbour on the shared network, so they are ignored.
2. **Signature, issuer, and audience**: RS256 only, key from the provider JWKS,
   exact issuer, audience matching the provider's client ID. Without the
   audience check, a token Authentik minted for another application's proxy
   provider, signed by the same certificate, would be accepted here. The check
   only helps if the audience is the portal's alone (contract item 13).
3. **Group-based roles**: only `fo-admin` and `fo-viewer` grant access. Broader
   homelab groups grant nothing.

### Current implementation status

Implemented in `app/middleware/authentik.py` and covered by
`tests/unit/test_middleware.py` (100% line and branch coverage of the module at
the time of writing). The `#ASSUME` items below have not yet been checked
against a deployed Authentik outpost.

### Residual risks

- **Token claims and algorithm unverified** `#ASSUME`: the claim names, `kid`
  header, and RS256 signing are taken from Authentik's documented behaviour.
  `#VERIFY` before first production use by decoding a real `X-authentik-jwt`
  from the deployed outpost and comparing it with the portal settings.
- **JWKS over https** `#ASSUME`: homelab-infra exposes the JWKS URL over TLS.
  `#VERIFY` with `curl` from inside the portal container.
- **Token replay**: a token copied from a legitimate request is valid until its
  `exp`. The exposure window is Authentik's token lifetime; short lifetimes are
  a homelab-infra setting.
- **Key rotation**: a rotated key is picked up on the first request carrying
  the new `kid` once 30 seconds have passed since the last fetch, or at the
  latest when the cache TTL expires.
- **JWKS outage** `#EDGE`: while the JWKS cannot be fetched, the last good key
  set keeps validating tokens until two full TTLs after the last successful
  fetch; with a cold cache, or after that grace period, every request gets
  403. The grace period trades prompt revocation for availability: a key
  Authentik removed after a compromise stays trusted for up to
  `2 * AUTHENTIK_JWKS_CACHE_SECONDS` if the JWKS is also unreachable.
  `#VERIFY` that this delay is acceptable when homelab-infra sets the cache
  lifetime; lowering `AUTHENTIK_JWKS_CACHE_SECONDS` shortens it.

## Validation

### Success Criteria

- [ ] Primary users sign in to Authentik without a password and reach the portal
- [ ] Sessions persist on their tablets without unexpected sign-in prompts
- [ ] Members of `fo-admin` reach `/admin` routes; members of `fo-viewer` get 403
- [ ] A request sent directly to the portal container with forged plain
      `X-authentik-*` headers gets 403
- [ ] A real `X-authentik-jwt` from the deployed outpost validates with the
      configured issuer, audience, and JWKS URL

### Review Schedule

- Initial: when homelab-infra first publishes the portal through Authentik
- Ongoing: if a primary user reports an unexpected sign-in prompt, or when
  Authentik's proxy provider or signing certificate changes

## Related

- [ADR-001](./adr-001-frontend-rendering-architecture.md): server-rendered app
  with no client-side auth handling
- [ADR-002](./adr-002-authentication-cloudflare-zero-trust.md): superseded;
  its user constraints carry over as requirements on homelab-infra
- [Tech Spec](../../planning/tech-spec.md#5-security): identity validation
  rules and environment variables
