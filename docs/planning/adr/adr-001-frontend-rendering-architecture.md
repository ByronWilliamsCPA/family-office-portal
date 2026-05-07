# ADR-001: Frontend Rendering Architecture

> **Status**: Accepted
> **Date**: 2026-05-06

## TL;DR

We will use server-rendered HTML (HTMX + Jinja2 + Tailwind CSS) rather than a JavaScript
SPA, because the primary users are low-tech and server rendering produces more predictable,
accessible behavior with standard browser navigation.

## Context

### Problem

The portal serves two users with very low technical proficiency on tablet devices. Every
interaction must behave exactly as they expect from a basic website: working back button,
no blank screens during load, no JavaScript errors, consistent navigation. The choice
of frontend architecture determines whether these guarantees can be made reliably.

### Constraints

- **Technical**: No existing frontend codebase; greenfield
- **Business**: Primary users have near-zero tolerance for friction; any frontend
  unpredictability creates a support call

### Significance

This is the highest-consequence architectural decision in the portal. Switching from
server-rendered to SPA mid-project would require rewriting every template, every route,
and every state management concern. The cost of getting this wrong is a complete frontend
rewrite.

## Decision

**We will use HTMX + Jinja2 + Tailwind CSS with server-rendered HTML because it
produces standard browser behavior, requires no client-side state management, and
degrades gracefully when JavaScript is unavailable or slow.**

### Rationale

Low-proficiency users rely on browser affordances (back button, bookmarks, copy URL) that
SPAs often break. Server rendering means each page is a complete, usable HTML document
with no hydration phase, no client-side routing edge cases, and no "white flash" on
initial load. HTMX handles partial updates (chart refreshes, search results) without
requiring a full JavaScript framework.

## Options Considered

### Option 1: HTMX + Jinja2 + Tailwind (Server-Rendered) ✓

**Pros**:

- Standard browser navigation works out of the box (back, forward, bookmarks)
- No client-side state management; fewer error surfaces
- Fast initial page load -- no JavaScript bundle to parse before rendering
- Works on slow tablet connections; partial failure shows partial content
- No framework upgrade churn

**Cons**:

- Full-page navigations feel less instant than SPA transitions
- Chart libraries (if needed) require lightweight JS wrappers

### Option 2: React / Next.js SPA

**Pros**:

- Rich interactivity for chart exploration

**Cons**:

- Hydration failures show blank content to users who cannot diagnose them
- Client-side routing breaks standard back-button behavior unless carefully managed
- Build toolchain complexity disproportionate to the portal's interaction requirements
- Significantly more JavaScript expertise required for maintenance

### Option 3: Vue or Svelte SPA

Similar trade-offs to Option 2. Lighter than React but still client-side routing,
hydration concerns, and a JavaScript build pipeline the project doesn't need.

## Consequences

### Positive

- Reliable behavior on tablet browsers with predictable network conditions
- Standard browser history; back button always works correctly
- Backend can own all data-fetching logic; frontend is purely presentation
- Simpler local development; no `npm run dev` required for the server

### Trade-offs

- HTMX partial-update responses require separate Jinja2 partials for each
  dynamic region; mitigation: establish a `partials/` template convention early
- Interactive charts (Portfolio performance) need a lightweight JS library
  (e.g., Chart.js) loaded as a static asset, not via npm

### Technical Debt

- If real-time push notifications are added later, WebSocket integration with HTMX
  requires the `hx-ext="ws"` extension -- plan for this at the HTMX config layer

## Implementation

### Components Affected

1. **App server**: Must render Jinja2 templates and serve HTMX partial responses
2. **Static assets**: Tailwind CSS compiled at build time; Chart.js loaded as CDN or
   vendored static file; HTMX loaded as static script
3. **Route layer**: Every page route returns a full HTML document; partial routes
   (used by HTMX `hx-get`) return fragment HTML only

### Testing Strategy

- Unit: Template rendering with mock data (pytest + Jinja2 environment)
- Integration: Full page loads with realistic backend fixture data
- E2E: Playwright against tablet viewport (1024x768); test all five sections

## Validation

### Success Criteria

- [ ] Back button navigates correctly across all five sections
- [ ] First meaningful paint < 1.5 seconds on a 10 Mbps connection (tablet)
- [ ] No JavaScript errors in browser console under normal operation
- [ ] All content is readable with JavaScript disabled (HTMX-enhanced features
  may degrade to full-page reload, which is acceptable)

### Review Schedule

- Initial: End of Phase 0 (first page rendered)
- Ongoing: If back-button failures or blank-page reports emerge from users

## Related

- [ADR-002](./adr-002-authentication-cloudflare-zero-trust.md): Auth integration
  uses server-side session validation, consistent with server-rendered approach
- [ADR-003](./adr-003-backend-data-aggregation.md): Data aggregation layer feeds
  template context directly
- [Tech Spec](../tech-spec.md#2-architecture): Component diagram references this decision
