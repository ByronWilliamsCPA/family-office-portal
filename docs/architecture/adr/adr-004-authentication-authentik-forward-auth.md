# ADR-004: Authentication via Authentik Forward Auth

> **Status**: Accepted
> **Date**: 2026-09-28
> **Supersedes**: [ADR-002](adr-002-authentication-cloudflare-zero-trust.md)

## TL;DR

The portal is published through Pangolin and Traefik on the homelab, and
Traefik's `authentik-chain@file` middleware authenticates every request with
Authentik. The portal validates the signed `X-authentik-jwt` header and maps
Authentik groups to its two roles. The portal holds no passwords, sessions, or
OAuth flows.

## Context

The family office is moving off Cloudflare Zero Trust tunnels to Pangolin for
remote access and Authentik for identity. The homelab already exposes apps
this way: Traefik forwards each request to the Authentik embedded outpost,
which returns identity headers (`X-authentik-username`, `X-authentik-groups`,
`X-authentik-email`, `X-authentik-jwt`) on success.

The primary users still have very low technical proficiency, so the
constraints from ADR-002 carry over: no passwords for them to manage, and long
device sessions.

## Decision

1. Traefik route `family-office.williamshome.family` uses
   `authentik-chain@file`. A forward-auth proxy provider and application are
   defined as an Authentik blueprint in homelab-infra.
2. The portal accepts identity only from the signed `X-authentik-jwt` header.
   It verifies the signature against the provider's JWKS
   (`AUTHENTIK_JWKS_URL`), and checks `iss` (`AUTHENTIK_ISSUER`), `aud`
   (`AUTHENTIK_AUDIENCE`), and `exp`.
3. The plain `X-authentik-username` and `X-authentik-groups` headers are
   ignored. The portal container shares the `traefik_proxy` network with other
   containers, any of which could send those headers.
4. Roles come from the `groups` claim: `fo-admin` grants Admin, `fo-viewer`
   grants Viewer (names configurable through `FO_ADMIN_GROUP` and
   `FO_VIEWER_GROUP`). Anyone else gets 403, including members of broad groups
   such as `homelab-family`.
5. `/health` and `/static/` are public; `/admin/*` requires Admin. The check
   fails closed: missing headers, invalid tokens, and an unreachable JWKS all
   return 403.
6. Primary users sign in to Authentik with passkeys (Face ID on their iPads).

## Consequences

- The portal no longer needs `CF_TEAM_DOMAIN`, `CF_ACCESS_APP_ID`,
  `VIEWER_EMAILS`, or `ADMIN_EMAILS`. Access changes are made in Authentik
  group membership, not in portal configuration.
- Portal availability depends on Pangolin, Traefik, and Authentik, all
  self-hosted. Their backup and restore procedure is part of the family office
  continuity plan.
- #VERIFY before first production use: decode a real `X-authentik-jwt` from
  the deployed outpost and confirm the `groups` claim, issuer, audience, and
  JWKS URL match the portal settings.

## Alternatives considered

- **Portal as an OIDC client of Authentik**: adds login, callback, logout, and
  session handling to a read-only app for no benefit over forward auth.
- **Trust the plain identity headers**: rejected; see decision 3.
