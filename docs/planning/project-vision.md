# Project Vision & Scope: Family Office Estate Portal

> **Status**: Active | **Version**: 1.0 | **Updated**: 2026-05-06

## TL;DR

A secure, read-only web portal that gives two low-tech family members consolidated visibility
into their estate: documents, entity compliance, finances, and portfolio performance -- all
through one URL with zero password management, presented in plain English.

## Problem Statement

### Pain Point

Two family members (the primary users) need regular visibility into a complex multi-entity
estate spanning legal entities, investment portfolios, compliance obligations, and decades
of estate planning documents. Today this requires navigating multiple professional systems,
logging into separate tools (Kubera, Portfolio Performance, Box), and calling advisors to
answer basic questions. The users are capable decision-makers locked out of their own
information.

### Target Users

- **Primary**: Two parents -- very low technical proficiency, comfortable with tablets and
  basic web browsing, usage pattern is occasional check-ins a few times per week
- **Admin**: 1-2 family managers -- moderately technical, handle document uploads and
  data monitoring; admin views can be utilitarian

### Success Metrics

- Document retrieval: < 2 minutes without assistance (currently: requires a call)
- Net worth check: self-service with no calls (currently: requires advisor contact)
- Entity compliance visibility: at-a-glance from home screen (currently: requires
  navigating professional tools)
- System reliability: 2+ weeks without manual intervention or support request
- User satisfaction: both parents describe portal as easy to use without prompting

## Solution Overview

### Core Value

One URL with Cloudflare magic-link login that shows the family's complete estate --
documents, entities, finances, and portfolio -- in plain English, read-only, with
graceful degradation when any backend is slow.

### Key Capabilities (MVP)

1. **Authenticated dashboard**: Cloudflare Zero Trust with email magic link; no password
   to remember; 30-day sessions on trusted devices
2. **Estate documents**: browse by category, search by name, download and preview PDFs
   inline -- all documents accessible within two navigation steps
3. **Entity compliance**: LLC and trust status at a glance (green/yellow/red), next key
   dates, ownership summary -- powered by `llc-manager`
4. **Financial summary**: net worth, asset allocation, account balances from Kubera data
   via `xero_crypto` and portfolio data via `pp-security-master`
5. **Resilient data display**: cached data with staleness timestamps when backends are
   slow; never a blank section or unhandled error

## Scope Definition

### In Scope (MVP)

- Home dashboard with net worth, upcoming compliance dates, portfolio snapshot,
  recent documents
- Documents section: category folders, name search, inline PDF preview, download
- Finances section: net worth chart, asset allocation, account list in USD
- Portfolio section: performance chart vs S&P 500, holdings table (plain names),
  sector allocation
- Entities section: status list, per-entity detail, linked documents
- Cloudflare Zero Trust authentication (magic link, 30-day device sessions)
- Two access levels: Viewer (read-only) and Admin
- Tablet-first responsive layout (iPad and desktop); phone layout out of scope for v1
- Staleness indicators and graceful degraded states for all sections

### Out of Scope

- ❌ **Ask / Q&A feature**: deferred to a future phase; `family_office` backend exists
  but NL interface is excluded from v1
- ❌ **Direct connections to Kubera, Google Drive, or Portfolio Performance UI**: all data
  is served by backend services, not fetched by the portal directly
- ❌ **Any write operations for primary users**: portal is read-only; no form submissions,
  edits, or deletes for the Viewer role
- ❌ **Phone-optimized layout**: tablet and desktop are the targets for initial release
- ❌ **Full-text document search**: name search only in v1; full-text is a future state
- ❌ **Database schema, API design, data ingestion pipelines, LLM infrastructure**: backend
  team concerns, not portal concerns

## Constraints

### Technical

- **Frontend**: HTMX + Jinja2 + Tailwind CSS; server-rendered HTML, no SPA patterns
- **Authentication**: Cloudflare Zero Trust (magic link only; no password credentials)
- **Backend services**: `llc-manager` v0.1.0, `pp-security-master` (alpha),
  `xero_crypto` v1.0.0, `family_office` (active) -- portal is a read-only consumer
- **Navigation depth**: maximum two levels anywhere in the information architecture
- **Top nav**: exactly five sections; no sub-menus
- **Device**: tablet (iPad-class) primary; desktop secondary

### Business

- **Access model**: family-internal only; no public registration or multi-tenant concerns
- **Reliability target**: 2-week autonomous operation; any backend failure must degrade
  gracefully, not surface as an error to primary users

## Assumptions to Validate

- [ ] Cloudflare Zero Trust access policy for the family email domain is already
  configured or will be configured before Phase 0 completes
- [ ] Each backend service exposes a stable internal API (not just CLI or direct DB
  access) that the portal can consume over HTTP
- [ ] `pp-security-master` alpha status is acceptable for read-only display with a
  staleness label; data accuracy issues will show as stale, not as incorrect data
- [ ] PDF documents are accessible via a URL the portal can proxy (not filesystem paths)
- [ ] Tablet landscape orientation is the baseline; portrait is a nice-to-have

## Related Documents

- [Architecture Decisions](./adr/)
- [Technical Spec](./tech-spec.md)
- [Roadmap](./roadmap.md)
