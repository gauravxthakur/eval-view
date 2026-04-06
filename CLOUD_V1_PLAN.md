# EvalView Cloud v1 — Tight Implementation Plan

**Stack: Next.js 14 (App Router) · TypeScript · Supabase (Auth + Postgres) · Stripe · Vercel**
*Scoped to 6-week alpha. Includes billing + security hardening. No golden sync, no hosted judge.*

---

## What v1 Is

A cloud dashboard that gives EvalView CLI users:

1. **Persistent run history** — every `evalview check` result, stored and queryable
2. **Team visibility** — multiple people can see the same project's results
3. **Shareable links** — send a run URL to a teammate or paste it in a PR
4. **Browser-based diff review** — the diff viewer from the CLI, but interactive
5. **Project-level trends** — pass rate, cost, and latency over time

That's it. No golden sync, no webhook automation.

6. **Billing from day one** — Stripe Checkout, free tier with hard limits, paid Team tier
7. **Security hardening** — rate limiting, CORS, CSP, input validation, audit logging

## What v1 Is NOT

| Deferred | Why |
|----------|-----|
| Bidirectional golden sync | Write conflicts without merge strategy = support nightmare. Goldens stay local and in git. |
| GitHub App / cloud-side PR comments | Prove dashboard value first. CLI-side `action.yml` with `--cloud` flag already posts comments. |
| Hosted judge | Adds cost, abuse risk, quota management. Users have their own API keys. |
| Realtime subscriptions | Nice-to-have. Polling or manual refresh is fine for alpha. |
| Slack/Discord alerts | CLI `evalview monitor --slack-webhook` already works locally. |
| SSO / SAML / enterprise | No enterprise customers yet. |
| Self-hosted option | Fragments support surface. Cloud-only. |

---

## Architecture

```
User's Machine / CI
┌─────────────────────────────────────────────────┐
│  evalview check                                  │
│       │                                          │
│       ├── executes agent locally                 │
│       ├── diffs against local golden baselines   │
│       ├── produces GateResult                    │
│       │                                          │
│       └── POST GateResult JSON ──────────────────┼──► EvalView Cloud
│           (best-effort, post-check,              │
│            agent keys never leave machine)        │
└─────────────────────────────────────────────────┘

EvalView Cloud (Vercel + Supabase + Stripe)
┌─────────────────────────────────────────────────┐
│                                                  │
│   Next.js App Router                             │
│   ├── /api/v1/results    (API token auth)        │
│   ├── /api/v1/projects   (session auth)          │
│   ├── /api/v1/tokens     (session auth)          │
│   ├── /api/v1/billing/*  (session + Stripe sig)  │
│   └── /(dashboard)/*     (session auth)          │
│                                                  │
│   Security layers:                               │
│   ├── Rate limiter (token bucket, per API token) │
│   ├── CORS whitelist (evalview.com only)         │
│   ├── CSP headers (strict)                       │
│   ├── Zod validation on every input              │
│   └── Audit log (security-relevant events)       │
│                                                  │
│   Supabase                                       │
│   ├── Auth (GitHub OAuth)                        │
│   ├── Postgres (RLS on all tables)               │
│   └── Encrypted at rest (Supabase default AES)   │
│                                                  │
│   Stripe                                         │
│   ├── Checkout Sessions                          │
│   ├── Subscription management                    │
│   ├── Webhook (signature-verified)               │
│   └── Customer portal (self-serve billing)       │
│                                                  │
│   Auth model:                                    │
│   ├── Dashboard → Supabase session cookies       │
│   │   (HttpOnly, Secure, SameSite=Lax)           │
│   └── CLI/CI   → API token → service-role        │
│       (API token writes NEVER use client-side     │
│        Supabase. They go through Next.js route    │
│        handlers using service-role client.)       │
│                                                  │
└─────────────────────────────────────────────────┘
```

**Key boundary:** CLI/CI requests are authenticated with project-scoped API tokens. The route handler verifies the token, then uses a **service-role Supabase client** to write to the database. This is not RLS-gated — RLS protects dashboard (human) access only. API-token access is server-validated, never client-side.

---

## Supabase Schema

Three migrations. Core tables, RLS, and billing/security.

```sql
-- 00001_v1_schema.sql

-- ============================================================
-- Organizations
-- ============================================================
CREATE TABLE organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    slug        TEXT UNIQUE NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free',     -- free | team | enterprise
    stripe_customer_id     TEXT UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Organization members
-- ============================================================
CREATE TABLE org_members (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'member',  -- owner, admin, member
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, user_id)
);

CREATE INDEX idx_org_members_user ON org_members(user_id);

-- ============================================================
-- Projects
-- ============================================================
CREATE TABLE projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',  -- active | archived (archived on downgrade)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, slug)
);

-- ============================================================
-- API tokens (CLI/CI authentication)
-- ============================================================
CREATE TABLE api_tokens (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,               -- "CI token", "local dev", etc.
    token_hash   TEXT UNIQUE NOT NULL,        -- SHA-256. Never store raw token.
    token_prefix TEXT NOT NULL,               -- First 8 chars for display: "ev_abc1..."
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by   UUID NOT NULL REFERENCES auth.users(id),
    revoked_at   TIMESTAMPTZ                 -- soft-delete; NULL = active
);

CREATE INDEX idx_api_tokens_hash ON api_tokens(token_hash)
    WHERE revoked_at IS NULL;

-- ============================================================
-- Runs (one row per `evalview check` execution)
--
-- DESIGN NOTE on result_json / diff_json:
--   These JSONB columns are APPEND-ONLY DEBUG BLOBS.
--   They exist for the "show raw JSON" button in the UI
--   and for future schema migrations where we need to
--   backfill new first-class columns.
--
--   ALL queryable data lives in first-class columns.
--   Never build queries, filters, or aggregations on
--   result_json or diff_json. If you need a field from
--   the JSON, promote it to a column first.
-- ============================================================
CREATE TABLE runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL,            -- CLI-generated run ID (for dedup)
    status          TEXT NOT NULL,            -- passed | regression | tools_changed | output_changed
    source          TEXT NOT NULL DEFAULT 'cli',  -- cli | ci | sdk | monitor

    -- Git context (nullable — not all runs come from git-tracked dirs)
    git_sha         TEXT,
    git_branch      TEXT,
    git_pr          INTEGER,

    -- First-class summary fields (what the dashboard queries)
    total_tests     INTEGER NOT NULL DEFAULT 0,
    unchanged       INTEGER NOT NULL DEFAULT 0,
    regressions     INTEGER NOT NULL DEFAULT 0,
    tools_changed   INTEGER NOT NULL DEFAULT 0,
    output_changed  INTEGER NOT NULL DEFAULT 0,
    total_cost      REAL DEFAULT 0.0,
    total_latency_ms REAL DEFAULT 0.0,

    -- Debug blob (see design note above)
    result_json     JSONB,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      UUID REFERENCES auth.users(id),
    deleted_at      TIMESTAMPTZ,             -- soft-delete for retention cleanup

    UNIQUE(project_id, run_id)               -- dedup repeated pushes
);

CREATE INDEX idx_runs_project_created ON runs(project_id, created_at DESC);
CREATE INDEX idx_runs_status ON runs(status);

-- ============================================================
-- Test diffs (one row per test within a run)
-- ============================================================
CREATE TABLE test_diffs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    test_name           TEXT NOT NULL,
    status              TEXT NOT NULL,        -- passed | regression | tools_changed | output_changed

    -- First-class diff fields
    score_delta         REAL DEFAULT 0.0,
    output_similarity   REAL,
    tool_changes        INTEGER DEFAULT 0,
    model_changed       BOOLEAN DEFAULT FALSE,

    -- Debug blob
    diff_json           JSONB,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_test_diffs_run ON test_diffs(run_id);
CREATE INDEX idx_test_diffs_name ON test_diffs(test_name);

-- ============================================================
-- Helpers
-- ============================================================

-- updated_at trigger
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at_orgs
    BEFORE UPDATE ON organizations FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER set_updated_at_projects
    BEFORE UPDATE ON projects FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

-- Membership check (used by RLS)
CREATE OR REPLACE FUNCTION is_org_member(p_org_id UUID)
RETURNS BOOLEAN AS $$
    SELECT EXISTS (
        SELECT 1 FROM org_members
        WHERE org_id = p_org_id AND user_id = auth.uid()
    );
$$ LANGUAGE sql SECURITY DEFINER STABLE;

CREATE OR REPLACE FUNCTION is_org_admin(p_org_id UUID)
RETURNS BOOLEAN AS $$
    SELECT EXISTS (
        SELECT 1 FROM org_members
        WHERE org_id = p_org_id AND user_id = auth.uid()
        AND role IN ('owner', 'admin')
    );
$$ LANGUAGE sql SECURITY DEFINER STABLE;
```

### RLS Policies

These protect **dashboard (human) access only**. CLI/CI writes go through service-role.

```sql
-- 00002_rls.sql

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_diffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_tokens ENABLE ROW LEVEL SECURITY;

-- Organizations: members can read, admins can update
CREATE POLICY orgs_select ON organizations FOR SELECT
    USING (is_org_member(id));
CREATE POLICY orgs_insert ON organizations FOR INSERT
    WITH CHECK (TRUE);  -- anyone can create
CREATE POLICY orgs_update ON organizations FOR UPDATE
    USING (is_org_admin(id));

-- Members: org members can read, admins can manage
CREATE POLICY members_select ON org_members FOR SELECT
    USING (is_org_member(org_id));
CREATE POLICY members_insert ON org_members FOR INSERT
    WITH CHECK (is_org_admin(org_id) OR user_id = auth.uid());
CREATE POLICY members_delete ON org_members FOR DELETE
    USING (is_org_admin(org_id));

-- Projects: org members read, members write (any member can create a project)
CREATE POLICY projects_select ON projects FOR SELECT
    USING (is_org_member(org_id));
CREATE POLICY projects_insert ON projects FOR INSERT
    WITH CHECK (is_org_member(org_id));
CREATE POLICY projects_update ON projects FOR UPDATE
    USING (is_org_admin(org_id));

-- Runs: org members can read non-deleted runs. Inserts come from service-role (CLI/CI).
CREATE POLICY runs_select ON runs FOR SELECT
    USING (
        runs.deleted_at IS NULL
        AND EXISTS (
            SELECT 1 FROM projects p
            WHERE p.id = runs.project_id AND is_org_member(p.org_id)
        )
    );

-- Test diffs: same read pattern as runs
CREATE POLICY test_diffs_select ON test_diffs FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM runs r
        JOIN projects p ON r.project_id = p.id
        WHERE r.id = test_diffs.run_id AND is_org_member(p.org_id)
    ));

-- API tokens: org members can read (prefix only in app), admins manage
CREATE POLICY tokens_select ON api_tokens FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM projects p
        WHERE p.id = api_tokens.project_id AND is_org_member(p.org_id)
    ));
CREATE POLICY tokens_insert ON api_tokens FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM projects p
        WHERE p.id = api_tokens.project_id AND is_org_admin(p.org_id)
    ));

-- NOTE: No INSERT policies on runs or test_diffs for anon/authenticated role.
-- All writes to these tables come through service-role in API route handlers.
-- This is intentional — CLI/CI auth is API-token-based, not Supabase-session-based.
```

### Migration 00003: Billing & Security

```sql
-- 00003_billing_security.sql

-- ============================================================
-- Usage tracking (per org, per billing period)
-- ============================================================
-- ============================================================
-- BILLING METRIC: "runs" = invocations of `evalview check`.
--
-- One `evalview check` = one run, regardless of how many tests
-- are in the suite. A 20-test suite counts as 1 run, not 20.
-- This is what users expect and what's easy to reason about.
-- ============================================================
CREATE TABLE usage (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    period      TEXT NOT NULL,                    -- "2026-03" (YYYY-MM)
    runs        INTEGER NOT NULL DEFAULT 0,      -- count of check invocations, NOT test cases
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, period)
);

CREATE INDEX idx_usage_org_period ON usage(org_id, period);

-- Atomic usage increment (called from route handlers via service-role)
-- Always increment by 1 (one run = one evalview check invocation)
CREATE OR REPLACE FUNCTION increment_usage(
    p_org_id UUID
) RETURNS INTEGER AS $$
DECLARE
    current_runs INTEGER;
BEGIN
    INSERT INTO usage (org_id, period, runs)
    VALUES (p_org_id, to_char(now(), 'YYYY-MM'), 1)
    ON CONFLICT (org_id, period)
    DO UPDATE SET
        runs = usage.runs + 1,
        updated_at = now()
    RETURNING runs INTO current_runs;

    RETURN current_runs;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Plan limits (queryable, immutable reference data)
CREATE TABLE plan_limits (
    plan             TEXT PRIMARY KEY,            -- free | team | enterprise
    max_runs         INTEGER NOT NULL,            -- per month, -1 = unlimited
    max_projects     INTEGER NOT NULL,
    max_members      INTEGER NOT NULL,
    retention_days   INTEGER NOT NULL
);

INSERT INTO plan_limits VALUES
    ('free',       100,   2,  1,   7),
    ('team',      10000, -1,  10,  90),
    ('enterprise', -1,   -1, -1,  365);

-- ============================================================
-- Rate limiting (sliding window, per API token)
-- ============================================================
CREATE TABLE rate_limits (
    token_hash  TEXT NOT NULL,
    window      TIMESTAMPTZ NOT NULL,            -- truncated to minute
    count       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (token_hash, window)
);

-- Check + increment rate limit. Returns TRUE if request is allowed.
CREATE OR REPLACE FUNCTION check_rate_limit(
    p_token_hash TEXT,
    p_max_per_minute INTEGER DEFAULT 60
) RETURNS BOOLEAN AS $$
DECLARE
    current_count INTEGER;
BEGIN
    INSERT INTO rate_limits (token_hash, window, count)
    VALUES (p_token_hash, date_trunc('minute', now()), 1)
    ON CONFLICT (token_hash, window)
    DO UPDATE SET count = rate_limits.count + 1
    RETURNING count INTO current_count;

    RETURN current_count <= p_max_per_minute;
END;
$$ LANGUAGE plpgsql;

-- Cleanup old rate limit windows (run daily via pg_cron or app-level cron)
CREATE OR REPLACE FUNCTION cleanup_rate_limits()
RETURNS void AS $$
BEGIN
    DELETE FROM rate_limits WHERE window < now() - INTERVAL '2 hours';
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Audit log (security-relevant events only)
--
-- DESIGN NOTE: This is NOT a general activity log.
-- Only log events that matter for incident response:
-- auth failures, token creation/revocation, plan changes,
-- admin role changes, rate limit breaches.
-- ============================================================
CREATE TABLE audit_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID REFERENCES organizations(id) ON DELETE SET NULL,
    actor_id    UUID,                            -- user or NULL for system events
    actor_ip    INET,
    event       TEXT NOT NULL,                   -- auth.login_failed, token.created, etc.
    detail      JSONB,                           -- event-specific context
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_log_org ON audit_log(org_id, created_at DESC);
CREATE INDEX idx_audit_log_event ON audit_log(event, created_at DESC);

-- Auto-purge audit logs older than 90 days (adjust for compliance needs)
CREATE OR REPLACE FUNCTION cleanup_audit_log()
RETURNS void AS $$
BEGIN
    DELETE FROM audit_log WHERE created_at < now() - INTERVAL '90 days';
END;
$$ LANGUAGE plpgsql;

-- RLS for new tables
ALTER TABLE usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

-- Usage: org members can read their own
CREATE POLICY usage_select ON usage FOR SELECT
    USING (is_org_member(org_id));

-- Audit log: org admins can read their own
CREATE POLICY audit_select ON audit_log FOR SELECT
    USING (is_org_admin(org_id));

-- No client-side INSERT policies on usage, rate_limits, or audit_log.
-- All writes go through service-role in route handlers.
```

---

## Security Hardening

### Threat Model

| Threat | Impact | Mitigation |
|--------|--------|------------|
| **Stolen API token** | Attacker pushes fake results to project | Token revocation, rate limiting, audit log, IP logging |
| **Brute-force token guessing** | Access to project data | 60 req/min rate limit on all API-token routes. Tokens are 32-byte random (256-bit entropy). Timing-safe comparison via SHA-256 hash lookup. |
| **XSS in diff viewer** | Session hijack, data exfiltration | CSP headers (no inline scripts), React's default escaping, DOMPurify for `diff_json` raw display |
| **CSRF on dashboard actions** | Token creation, project deletion by tricked user | SameSite=Lax cookies + explicit Origin/Referer header verification on all session-authed POST/DELETE route handlers (see `verifyCsrf()` below). CORS is NOT sufficient — it only blocks reading the response, not sending the request. |
| **Injection via result payloads** | SQL injection, stored XSS | Zod validation on all inputs, parameterized queries (Supabase client), JSONB stored as data not executable |
| **Supabase service-role key leak** | Full DB access | Key only in Vercel env vars (encrypted at rest), never in client bundle (`SUPABASE_` not `NEXT_PUBLIC_`), key rotation SOP |
| **Stripe webhook spoofing** | Fake plan upgrades | HMAC signature verification on every webhook (`stripe.webhooks.constructEvent`) |
| **DDoS on result ingestion** | Service degradation | Rate limiting (DB-level), Vercel Edge middleware for IP-level throttling, payload size limit (1MB) |
| **Data exfiltration via API** | Competitor reads your test data | RLS on all tables, API tokens scoped to single project, no cross-project queries possible |
| **Session fixation/hijack** | Account takeover | HttpOnly + Secure + SameSite cookies, Supabase handles token rotation on refresh |

### Security Headers (Next.js Middleware)

```typescript
// src/middleware.ts
import { NextResponse, type NextRequest } from "next/server";
import { updateSession } from "@/lib/supabase/middleware";

// Security headers applied to ALL responses
const securityHeaders = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-XSS-Protection": "0",                     // disabled; CSP is the real protection
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
  // CSP: PRODUCTION config. No 'unsafe-eval'.
  // Next.js uses 'unsafe-eval' in development mode only (hot reload).
  // In production builds, Next.js does NOT require 'unsafe-eval'.
  // If you see CSP violations in production, fix the code — do not re-add unsafe-eval.
  "Content-Security-Policy": [
    "default-src 'self'",
    "script-src 'self'",                        // NO unsafe-eval in production. Period.
    "style-src 'self' 'unsafe-inline'",         // Tailwind needs this (CSS-in-JS injects style tags)
    "img-src 'self' data: https:",
    "font-src 'self'",
    "connect-src 'self' https://*.supabase.co https://api.stripe.com",
    "frame-src https://js.stripe.com",          // Stripe Checkout iframe
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; "),
};

export async function middleware(request: NextRequest) {
  // 1. Auth session refresh
  const response = await updateSession(request);

  // 2. Apply security headers
  for (const [key, value] of Object.entries(securityHeaders)) {
    response.headers.set(key, value);
  }

  // 3. CORS for API routes (restrict to evalview.com + CLI user-agents)
  if (request.nextUrl.pathname.startsWith("/api/")) {
    const origin = request.headers.get("origin");
    const allowedOrigins = [
      "https://evalview.com",
      "https://www.evalview.com",
    ];
    if (origin && allowedOrigins.includes(origin)) {
      response.headers.set("Access-Control-Allow-Origin", origin);
      response.headers.set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
      response.headers.set("Access-Control-Allow-Headers", "Authorization, Content-Type");
      response.headers.set("Access-Control-Max-Age", "86400");
    }
    // CLI/CI requests have no Origin header — that's fine, they use API tokens
  }

  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
```

### Rate Limiting

```typescript
// src/lib/rate-limit.ts
import { createAdminClient } from "@/lib/supabase/admin";

/**
 * Check rate limit for an API token. Returns true if allowed.
 *
 * Uses DB-level sliding window (per minute) to survive serverless cold starts.
 * Vercel Edge middleware handles IP-level throttling separately.
 */
export async function checkRateLimit(
  tokenHash: string,
  maxPerMinute: number = 60
): Promise<boolean> {
  const supabase = createAdminClient();
  const { data, error } = await supabase.rpc("check_rate_limit", {
    p_token_hash: tokenHash,
    p_max_per_minute: maxPerMinute,
  });

  if (error) {
    // Fail open on DB errors — don't block legitimate traffic
    // because rate_limits table is unhealthy. Log for alerting.
    console.error("Rate limit check failed:", error);
    return true;
  }

  return data === true;
}
```

### Audit Logging

```typescript
// src/lib/audit.ts
import { createAdminClient } from "@/lib/supabase/admin";
import { headers } from "next/headers";

type AuditEvent =
  | "auth.login"
  | "auth.login_failed"
  | "auth.logout"
  | "token.created"
  | "token.revoked"
  | "project.created"
  | "project.deleted"
  | "member.invited"
  | "member.removed"
  | "member.role_changed"
  | "plan.upgraded"
  | "plan.downgraded"
  | "rate_limit.exceeded"
  | "api.unauthorized";

/**
 * Write a security audit event. Fire-and-forget — never blocks the request.
 */
export async function audit(
  event: AuditEvent,
  opts: {
    orgId?: string;
    actorId?: string;
    detail?: Record<string, unknown>;
  } = {}
): Promise<void> {
  try {
    const headerStore = await headers();
    const ip = headerStore.get("x-forwarded-for")?.split(",")[0]?.trim() ?? null;

    const supabase = createAdminClient();
    await supabase.from("audit_log").insert({
      org_id: opts.orgId ?? null,
      actor_id: opts.actorId ?? null,
      actor_ip: ip,
      event,
      detail: opts.detail ?? null,
    });
  } catch (e) {
    // Never fail a request because audit logging broke
    console.error("Audit log failed:", e);
  }
}
```

### CSRF Protection for Session-Authed Routes

CORS blocks cross-origin **reads**, not cross-origin **writes**. A malicious page can still POST to your API with the user's cookies attached. SameSite=Lax helps (blocks cross-site POST in most browsers) but is not sufficient alone — it doesn't cover same-site subdomain attacks and some browser edge cases.

Every session-authenticated mutation route (POST, DELETE) must call `verifyCsrf()`:

```typescript
// src/lib/csrf.ts
import { NextRequest } from "next/server";

const ALLOWED_ORIGINS = new Set([
  "https://evalview.com",
  "https://www.evalview.com",
]);

// In development, also allow localhost
if (process.env.NODE_ENV === "development") {
  ALLOWED_ORIGINS.add("http://localhost:3000");
}

/**
 * Verify that the request originated from our own frontend.
 *
 * Checks the Origin header (set by browsers on all POST/DELETE requests).
 * Falls back to Referer if Origin is missing (some privacy proxies strip it).
 * Rejects if neither header matches an allowed origin.
 *
 * This is NOT needed on API-token-authed routes (CLI/CI) because those
 * don't use cookies — there's nothing to CSRF.
 */
export function verifyCsrf(req: NextRequest): boolean {
  const origin = req.headers.get("origin");
  if (origin) {
    return ALLOWED_ORIGINS.has(origin);
  }

  // Fallback: check Referer header
  const referer = req.headers.get("referer");
  if (referer) {
    try {
      const refOrigin = new URL(referer).origin;
      return ALLOWED_ORIGINS.has(refOrigin);
    } catch {
      return false;
    }
  }

  // No Origin or Referer — reject. Legitimate browser requests always send one.
  return false;
}
```

Usage in session-authed route handlers:

```typescript
// Example: POST /api/v1/tokens
export async function POST(req: NextRequest) {
  if (!verifyCsrf(req)) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }
  // ... rest of handler
}
```

### Input Validation Rules

All API route handlers enforce:

| Rule | Implementation |
|------|---------------|
| **Payload size** | Explicit check: `if (req.headers.get("content-length") > 1_048_576) return 413`. App Router route handlers don't support `api.bodyParser.sizeLimit` (that's Pages Router). Enforce at the request layer. |
| **Schema validation** | Zod on every POST body. Invalid → 400 with structured error. No raw `req.body` usage anywhere. |
| **String length limits** | All text fields have `z.string().max(N)` — prevents megabyte-length strings in text columns |
| **Enum enforcement** | Status, source, role fields use `z.enum()` — no arbitrary values in constrained columns |
| **UUID format** | Path params validated as UUID format before DB query — prevents injection in parameterized queries |
| **No eval/interpolation** | No string interpolation in SQL (Supabase client handles parameterization). No `eval()`, no `new Function()`. |
| **JSONB blob limits** | `result_json` and `diff_json` are accepted as `z.any()` but the explicit 1MB Content-Length check caps total payload size. For tighter blob-specific control, add `z.string().max(500_000).transform(JSON.parse)`. |

### API Token Security

```typescript
// Token generation (called when creating a new token)
import { randomBytes, createHash } from "crypto";

export function generateApiToken(): { raw: string; hash: string; prefix: string } {
  // 32 bytes = 256 bits of entropy. Brute-force infeasible.
  const rawBytes = randomBytes(32);
  const raw = `ev_${rawBytes.toString("base64url")}`;
  const hash = createHash("sha256").update(raw).digest("hex");
  const prefix = raw.slice(0, 11);  // "ev_" + first 8 chars

  return { raw, hash, prefix };
  // raw: shown to user ONCE, never stored
  // hash: stored in api_tokens.token_hash
  // prefix: stored in api_tokens.token_prefix for display
}
```

- Tokens are 32-byte random (256-bit entropy) — computationally infeasible to brute force
- Only the SHA-256 hash is stored — a database breach does not reveal usable tokens
- Token lookup is by hash index — constant-time at the DB level (no timing oracle)
- Revocation is immediate (soft-delete, filtered in query)
- `last_used_at` tracking enables stale token detection

### Vercel Deployment Security

| Setting | Value | Why |
|---------|-------|-----|
| `SUPABASE_SERVICE_ROLE_KEY` | Vercel encrypted env var | Never in client bundle. `SUPABASE_` prefix (not `NEXT_PUBLIC_`) ensures Next.js excludes it from client JS. |
| `STRIPE_SECRET_KEY` | Vercel encrypted env var | Same — server-only. |
| `STRIPE_WEBHOOK_SECRET` | Vercel encrypted env var | Used to verify Stripe webhook signatures. |
| Preview deployments | Disabled for production env vars | Prevents service-role key from leaking into PR preview URLs. |
| Function regions | `iad1` only | Reduces attack surface. Single region = simpler firewall rules. |

---

## Billing (Stripe)

### Plan Definitions

| | Free (Starter) | Team ($49/mo) |
|---|---|---|
| **Runs / Month** | 100 | 10,000 |
| **Projects** | 2 | Unlimited |
| **Team Members** | 1 (just you) | 10 |
| **Result Retention** | 7 days | 90 days |
| **Support** | Community | Email |

Enterprise is not in v1. Add it when you have inbound demand.

Annual pricing: $39/mo billed yearly ($468/year). 20% discount anchors the monthly price.

### Stripe Integration

```typescript
// src/lib/stripe/client.ts
import Stripe from "stripe";

export const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!, {
  apiVersion: "2024-12-18.acacia",
  typescript: true,
});

// Price IDs (create these in Stripe Dashboard)
export const PRICES = {
  team_monthly: process.env.STRIPE_PRICE_TEAM_MONTHLY!,
  team_annual: process.env.STRIPE_PRICE_TEAM_ANNUAL!,
} as const;
```

### Checkout Flow

```typescript
// src/app/api/v1/billing/checkout/route.ts
import { NextRequest, NextResponse } from "next/server";
import { createServerSupabase } from "@/lib/supabase/server";
import { createAdminClient } from "@/lib/supabase/admin";
import { stripe, PRICES } from "@/lib/stripe/client";
import { audit } from "@/lib/audit";

export async function POST(req: NextRequest) {
  // 1. Verify session (dashboard auth, not API token)
  const supabase = await createServerSupabase();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { orgId, interval } = await req.json();  // interval: "monthly" | "annual"

  // 2. Verify user is org admin
  const { data: membership } = await supabase
    .from("org_members")
    .select("role")
    .eq("org_id", orgId)
    .eq("user_id", user.id)
    .single();

  if (!membership || !["owner", "admin"].includes(membership.role)) {
    return NextResponse.json({ error: "Only admins can manage billing" }, { status: 403 });
  }

  // 3. Get or create Stripe customer
  const admin = createAdminClient();
  const { data: org } = await admin
    .from("organizations")
    .select("stripe_customer_id, name")
    .eq("id", orgId)
    .single();

  let customerId = org?.stripe_customer_id;
  if (!customerId) {
    const customer = await stripe.customers.create({
      email: user.email,
      name: org?.name,
      metadata: { org_id: orgId, supabase_user_id: user.id },
    });
    customerId = customer.id;
    await admin
      .from("organizations")
      .update({ stripe_customer_id: customerId })
      .eq("id", orgId);
  }

  // 4. Create checkout session
  const priceId = interval === "annual" ? PRICES.team_annual : PRICES.team_monthly;

  const session = await stripe.checkout.sessions.create({
    customer: customerId,
    mode: "subscription",
    line_items: [{ price: priceId, quantity: 1 }],
    success_url: `${process.env.NEXT_PUBLIC_APP_URL}/${orgId}/billing?success=true`,
    cancel_url: `${process.env.NEXT_PUBLIC_APP_URL}/${orgId}/billing?canceled=true`,
    metadata: { org_id: orgId },
    subscription_data: { metadata: { org_id: orgId } },
  });

  await audit("plan.upgraded", {
    orgId,
    actorId: user.id,
    detail: { interval, checkout_session: session.id },
  });

  return NextResponse.json({ url: session.url });
}
```

### Stripe Webhook Handler

```typescript
// src/app/api/v1/billing/webhook/route.ts
import { NextRequest, NextResponse } from "next/server";
import { stripe } from "@/lib/stripe/client";
import { createAdminClient } from "@/lib/supabase/admin";
import { audit } from "@/lib/audit";
import Stripe from "stripe";

export async function POST(req: NextRequest) {
  const body = await req.text();
  const sig = req.headers.get("stripe-signature");

  if (!sig) {
    return NextResponse.json({ error: "Missing signature" }, { status: 400 });
  }

  // 1. Verify webhook signature (CRITICAL — prevents spoofed upgrades)
  let event: Stripe.Event;
  try {
    event = stripe.webhooks.constructEvent(
      body,
      sig,
      process.env.STRIPE_WEBHOOK_SECRET!
    );
  } catch (err) {
    console.error("Stripe webhook signature verification failed:", err);
    return NextResponse.json({ error: "Invalid signature" }, { status: 400 });
  }

  const supabase = createAdminClient();

  // 2. Handle events
  switch (event.type) {
    case "checkout.session.completed": {
      const session = event.data.object as Stripe.Checkout.Session;
      const orgId = session.metadata?.org_id;
      if (orgId && session.subscription) {
        await supabase
          .from("organizations")
          .update({
            plan: "team",
            stripe_customer_id: session.customer as string,
            stripe_subscription_id: session.subscription as string,
          })
          .eq("id", orgId);

        await audit("plan.upgraded", { orgId, detail: { to: "team" } });
      }
      break;
    }

    case "customer.subscription.deleted": {
      const sub = event.data.object as Stripe.Subscription;
      const orgId = sub.metadata?.org_id;
      await supabase
        .from("organizations")
        .update({
          plan: "free",
          stripe_subscription_id: null,
        })
        .eq("stripe_subscription_id", sub.id);

      if (orgId) {
        await audit("plan.downgraded", { orgId, detail: { to: "free", reason: "subscription_deleted" } });
      }
      break;
    }

    case "invoice.payment_failed": {
      const invoice = event.data.object as Stripe.Invoice;
      const orgId = (invoice.subscription_details?.metadata as any)?.org_id;
      // Don't downgrade immediately — Stripe retries for ~3 weeks.
      // Log for manual follow-up.
      if (orgId) {
        await audit("plan.downgraded", {
          orgId,
          detail: { reason: "payment_failed", invoice_id: invoice.id },
        });
      }
      break;
    }
  }

  return NextResponse.json({ received: true });
}
```

### Customer Portal (Self-Serve)

```typescript
// src/app/api/v1/billing/portal/route.ts
export async function POST(req: NextRequest) {
  const supabase = await createServerSupabase();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const { orgId } = await req.json();

  const admin = createAdminClient();
  const { data: org } = await admin
    .from("organizations")
    .select("stripe_customer_id")
    .eq("id", orgId)
    .single();

  if (!org?.stripe_customer_id) {
    return NextResponse.json({ error: "No billing account" }, { status: 404 });
  }

  const session = await stripe.billingPortal.sessions.create({
    customer: org.stripe_customer_id,
    return_url: `${process.env.NEXT_PUBLIC_APP_URL}/${orgId}/billing`,
  });

  return NextResponse.json({ url: session.url });
}
```

### Usage Enforcement

Checked on every `POST /api/v1/results` call:

```typescript
// src/lib/billing/usage.ts
import { createAdminClient } from "@/lib/supabase/admin";

// Billing metric: RUNS, not test cases.
// One `evalview check` = 1 run, regardless of suite size.
const PLAN_LIMITS: Record<string, number> = {
  free: 100,       // 100 runs/month
  team: 10_000,    // 10,000 runs/month
  enterprise: -1,  // unlimited
};

/**
 * Check usage BEFORE incrementing. Only call incrementUsage()
 * after confirming the run was actually inserted (not a duplicate).
 *
 * CONCURRENCY NOTE: This is a check-then-act pattern with a race
 * window. Two concurrent requests can both pass the check and both
 * increment, exceeding the cap by 1. This is acceptable for alpha:
 * - The overshoot is bounded (at most +N where N = concurrent requests)
 * - Billing is monthly, so a few extra runs don't matter materially
 * - The alternative (SELECT FOR UPDATE or single-function
 *   check+insert) adds latency and DB lock contention
 *
 * If this becomes a real problem at scale, consolidate into a single
 * Postgres function that checks the limit and increments atomically
 * within one transaction.
 */
export async function checkUsage(
  orgId: string,
  plan: string,
): Promise<{ allowed: boolean; remaining?: number; limit?: number }> {
  const limit = PLAN_LIMITS[plan] ?? PLAN_LIMITS.free;
  if (limit === -1) return { allowed: true };

  const supabase = createAdminClient();
  const period = new Date().toISOString().slice(0, 7); // "2026-03"

  const { data } = await supabase
    .from("usage")
    .select("runs")
    .eq("org_id", orgId)
    .eq("period", period)
    .single();

  const current = data?.runs ?? 0;

  if (current >= limit) {
    return { allowed: false, limit };
  }

  return { allowed: true, remaining: limit - current };
}

/**
 * Increment usage by 1 run. Call this ONLY after confirming
 * the run was a new insert (not an upsert that matched an existing row).
 */
export async function incrementUsage(orgId: string): Promise<void> {
  const supabase = createAdminClient();
  await supabase.rpc("increment_usage", { p_org_id: orgId });
}
```

### Downgrade Behavior

When subscription is canceled (via Stripe webhook or customer portal):

1. `organizations.plan` set to `'free'` immediately.
2. Existing data is **read-only for 14 days** — dashboard still works, but `POST /results` is capped at free limits (100 runs/mo).
3. After 14 days: runs older than 7 days (free retention) are soft-deleted via `runs.deleted_at = now()`. RLS policy filters them from dashboard queries. A weekly cleanup job hard-deletes runs where `deleted_at < now() - interval '30 days'`.
4. Projects beyond the 2-project free limit are set to `projects.status = 'archived'` — visible in dashboard (greyed out) but `POST /results` rejects pushes with `403 Project archived. Upgrade to re-activate.`
5. Extra members lose dashboard access but aren't removed from `org_members` — they regain access on re-upgrade.

---

## API Surface (v1 only)

9 endpoints. Clean auth boundary:

- **CLI/CI writes** → API token auth → service-role DB client (bypasses RLS)
- **Dashboard reads** → Supabase session auth → anon DB client (RLS-enforced)

Dashboard pages (run list, run detail, trends) read directly from Supabase via server components using the session client. No API routes needed for reads — RLS policies already scope data to the user's org. This eliminates a second read surface and keeps the API surface write-only for CLI/CI.

| Method | Path | Auth | Security | Purpose |
|--------|------|------|----------|---------|
| `POST` | `/api/v1/results` | API token | Rate limit + usage check | Push run results from CLI/CI |
| `POST` | `/api/v1/projects` | Session | Origin check | Create a project (returns project + first API token) |
| `POST` | `/api/v1/tokens` | Session | Origin check + audit log | Create an API token for a project |
| `DELETE` | `/api/v1/tokens/[id]` | Session | Origin check + audit log | Revoke a token |
| `POST` | `/api/v1/billing/checkout` | Session | Origin check + admin-only | Create Stripe Checkout session |
| `POST` | `/api/v1/billing/portal` | Session | Origin check + admin-only | Redirect to Stripe Customer Portal |
| `POST` | `/api/v1/billing/webhook` | Stripe signature | HMAC-SHA256 verification | Handle Stripe subscription events |
| `GET` | `/api/v1/billing/usage` | Session | RLS | Get current usage for org |

### POST /api/v1/results — The Critical Path

```typescript
// src/app/api/v1/results/route.ts
import { NextRequest, NextResponse } from "next/server";
import { verifyApiToken } from "@/lib/api-auth";
import { createAdminClient } from "@/lib/supabase/admin";
import { z } from "zod";

const ResultPayload = z.object({
  run_id: z.string().min(1).max(64),
  status: z.enum(["passed", "regression", "tools_changed", "output_changed"]),
  source: z.enum(["cli", "ci", "sdk", "monitor"]).default("cli"),
  git_sha: z.string().max(40).nullish(),
  git_branch: z.string().max(256).nullish(),
  git_pr: z.number().int().nullish(),
  summary: z.object({
    total: z.number().int().nonnegative(),
    unchanged: z.number().int().nonnegative(),
    regressions: z.number().int().nonnegative(),
    tools_changed: z.number().int().nonnegative(),
    output_changed: z.number().int().nonnegative(),
  }),
  total_cost: z.number().nonnegative().default(0),
  total_latency_ms: z.number().nonnegative().default(0),
  diffs: z.array(z.object({
    test_name: z.string(),
    status: z.string(),
    score_delta: z.number().default(0),
    output_similarity: z.number().nullable().default(null),
    tool_changes: z.number().int().default(0),
    model_changed: z.boolean().default(false),
    diff_json: z.any().optional(),           // debug blob, never queried
  })),
  result_json: z.any().optional(),           // debug blob, never queried
});

export async function POST(req: NextRequest) {
  // 1. Verify API token (not Supabase session — this is CLI/CI)
  const ctx = await verifyApiToken(req.headers.get("authorization"));
  if (!ctx) {
    await audit("api.unauthorized", {
      detail: { path: "/api/v1/results", method: "POST" },
    });
    return NextResponse.json({ error: "Invalid or expired API token" }, { status: 401 });
  }

  // 2. Rate limit (60 req/min per token)
  const allowed = await checkRateLimit(ctx.tokenHash);
  if (!allowed) {
    await audit("rate_limit.exceeded", {
      orgId: ctx.orgId,
      detail: { token_prefix: ctx.tokenHash.slice(0, 8) },
    });
    return NextResponse.json(
      { error: "Rate limit exceeded. Max 60 requests/minute." },
      { status: 429, headers: { "Retry-After": "60" } }
    );
  }

  // 3. Parse and validate
  const body = await req.json();
  const parsed = ResultPayload.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid payload", details: parsed.error.flatten() },
      { status: 400 }
    );
  }
  const p = parsed.data;

  // 4. Check usage limits BEFORE writing (free: 100 runs/mo, team: 10k/mo)
  // "Run" = one evalview check invocation, not number of tests in the suite.
  const usage = await checkUsage(ctx.orgId, ctx.plan);
  if (!usage.allowed) {
    return NextResponse.json(
      {
        error: `Monthly limit of ${usage.limit} runs reached. Upgrade at https://evalview.com/billing`,
        code: "USAGE_LIMIT_EXCEEDED",
      },
      { status: 402 }
    );
  }

  // 5. Write run via service-role client (bypasses RLS intentionally)
  const supabase = createAdminClient();

  // Try INSERT first to detect new vs duplicate
  const { data: inserted, error: insertErr } = await supabase
    .from("runs")
    .insert({
      project_id: ctx.projectId,
      run_id: p.run_id,
      status: p.status,
      source: p.source,
      git_sha: p.git_sha ?? null,
      git_branch: p.git_branch ?? null,
      git_pr: p.git_pr ?? null,
      total_tests: p.summary.total,
      unchanged: p.summary.unchanged,
      regressions: p.summary.regressions,
      tools_changed: p.summary.tools_changed,
      output_changed: p.summary.output_changed,
      total_cost: p.total_cost,
      total_latency_ms: p.total_latency_ms,
      result_json: p.result_json ?? null,
    })
    .select("id")
    .single();

  let runDbId: string;
  let isNewRun: boolean;

  if (insertErr?.code === "23505") {
    // Duplicate run_id — this is a retry. Update existing row instead.
    isNewRun = false;
    const { data: existing, error: updateErr } = await supabase
      .from("runs")
      .update({
        status: p.status,
        total_tests: p.summary.total,
        unchanged: p.summary.unchanged,
        regressions: p.summary.regressions,
        tools_changed: p.summary.tools_changed,
        output_changed: p.summary.output_changed,
        total_cost: p.total_cost,
        total_latency_ms: p.total_latency_ms,
        result_json: p.result_json ?? null,
      })
      .eq("project_id", ctx.projectId)
      .eq("run_id", p.run_id)
      .select("id")
      .single();

    if (updateErr || !existing) {
      console.error("Failed to update duplicate run:", updateErr);
      return NextResponse.json({ error: "Failed to save" }, { status: 500 });
    }
    runDbId = existing.id;
  } else if (insertErr) {
    console.error("Failed to insert run:", insertErr);
    return NextResponse.json({ error: "Failed to save" }, { status: 500 });
  } else {
    isNewRun = true;
    runDbId = inserted.id;
  }

  // 6. Handle test diffs (idempotent: delete existing, then re-insert)
  if (p.diffs.length > 0) {
    if (!isNewRun) {
      // Duplicate push — clear old diffs first to prevent duplication
      await supabase.from("test_diffs").delete().eq("run_id", runDbId);
    }
    await supabase.from("test_diffs").insert(
      p.diffs.map((d) => ({
        run_id: runDbId,
        test_name: d.test_name,
        status: d.status,
        score_delta: d.score_delta,
        output_similarity: d.output_similarity,
        tool_changes: d.tool_changes,
        model_changed: d.model_changed,
        diff_json: d.diff_json ?? null,
      }))
    );
  }

  // 7. Increment usage ONLY for new runs (not retries)
  if (isNewRun) {
    await incrementUsage(ctx.orgId);
  }

  // 8. Update token last_used_at
  await supabase
    .from("api_tokens")
    .update({ last_used_at: new Date().toISOString() })
    .eq("token_hash", ctx.tokenHash);

  // 9. Build correct dashboard URL with org/project slugs
  const { data: slugs } = await supabase
    .from("projects")
    .select("slug, organizations!inner(slug)")
    .eq("id", ctx.projectId)
    .single();

  const orgSlug = (slugs?.organizations as any)?.slug ?? ctx.orgId;
  const projectSlug = slugs?.slug ?? ctx.projectId;
  const dashboardUrl = `https://evalview.com/${orgSlug}/${projectSlug}/runs/${runDbId}`;

  return NextResponse.json(
    { id: runDbId, dashboard_url: dashboardUrl },
    { status: isNewRun ? 201 : 200 }
  );
}
```

### API Token Verification

```typescript
// src/lib/api-auth.ts
import { createHash } from "crypto";
import { createAdminClient } from "@/lib/supabase/admin";

export interface ApiContext {
  projectId: string;
  orgId: string;
  plan: string;
  tokenHash: string;
}

export async function verifyApiToken(
  authHeader: string | null
): Promise<ApiContext | null> {
  if (!authHeader?.startsWith("Bearer ev_")) return null;

  const token = authHeader.slice(7);
  const tokenHash = createHash("sha256").update(token).digest("hex");

  const supabase = createAdminClient();  // service-role, bypasses RLS

  const { data, error } = await supabase
    .from("api_tokens")
    .select("project_id, projects!inner(org_id, organizations!inner(plan))")
    .eq("token_hash", tokenHash)
    .is("revoked_at", null)
    .single();

  if (error || !data) return null;

  return {
    projectId: data.project_id,
    orgId: (data.projects as any).org_id,
    plan: (data.projects as any).organizations.plan,
    tokenHash,
  };
}
```

### Supabase Client Split

```typescript
// src/lib/supabase/admin.ts — SERVICE ROLE (for API-token-authed writes)
import { createClient } from "@supabase/supabase-js";

export function createAdminClient() {
  return createClient(
    process.env.SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!,  // bypasses RLS
    { auth: { persistSession: false } }
  );
}

// src/lib/supabase/server.ts — USER SESSION (for dashboard reads)
import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

export async function createServerSupabase() {
  const cookieStore = await cookies();
  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll: () => cookieStore.getAll(),
        setAll: (c) => { c.forEach(({ name, value, options }) => cookieStore.set(name, value, options)); },
      },
    }
  );
}
```

---

## Next.js App Structure (v1)

```
cloud/
├── package.json
├── next.config.ts
├── tailwind.config.ts
├── .env.local.example
│
├── supabase/
│   └── migrations/
│       ├── 00001_v1_schema.sql
│       ├── 00002_rls.sql
│       └── 00003_billing_security.sql
│
├── src/
│   ├── middleware.ts                    # Auth + security headers + CORS
│   ├── app/
│   │   ├── layout.tsx                   # html/body, font, tailwind
│   │   ├── (auth)/
│   │   │   ├── login/page.tsx           # GitHub OAuth button
│   │   │   └── callback/route.ts        # OAuth code → session
│   │   ├── (dashboard)/
│   │   │   ├── layout.tsx               # sidebar, org switcher, auth gate
│   │   │   ├── page.tsx                 # home: list projects, recent runs
│   │   │   ├── new-project/page.tsx     # create project → get API token
│   │   │   └── [orgSlug]/
│   │   │       ├── billing/page.tsx     # plan, usage meter, upgrade/manage
│   │   │       └── [projectSlug]/
│   │   │           ├── page.tsx         # project overview: test list + latest run
│   │   │           ├── runs/page.tsx    # paginated run history table
│   │   │           ├── runs/[runId]/page.tsx  # single run: diff viewer
│   │   │           ├── trends/page.tsx  # charts: pass rate, cost, latency
│   │   │           └── tokens/page.tsx  # create/revoke API tokens
│   │   └── api/v1/
│   │       ├── results/route.ts         # POST only (CLI/CI push, API token auth)
│   │       ├── projects/route.ts        # POST create project
│   │       ├── tokens/route.ts          # POST create token
│   │       ├── tokens/[id]/route.ts     # DELETE revoke token
│   │       └── billing/
│   │           ├── checkout/route.ts    # POST create Stripe checkout
│   │           ├── portal/route.ts      # POST redirect to Stripe portal
│   │           ├── webhook/route.ts     # POST Stripe webhook (sig verified)
│   │           └── usage/route.ts       # GET current usage for org
│   │
│   ├── lib/
│   │   ├── supabase/
│   │   │   ├── admin.ts                 # service-role client
│   │   │   ├── server.ts                # session client
│   │   │   ├── client.ts                # browser client
│   │   │   └── middleware.ts            # session refresh for Next.js
│   │   ├── stripe/
│   │   │   ├── client.ts               # Stripe SDK init + price IDs
│   │   │   └── usage.ts                # checkUsage() + incrementUsage()
│   │   ├── api-auth.ts                  # API token verification
│   │   ├── rate-limit.ts                # Token bucket rate limiter
│   │   ├── audit.ts                     # Security audit logger
│   │   └── types.ts                     # shared types
│   │
│   └── components/
│       ├── ui/                          # shadcn/ui primitives
│       ├── sidebar.tsx
│       ├── org-switcher.tsx
│       ├── project-card.tsx
│       ├── run-table.tsx
│       ├── run-status-badge.tsx
│       ├── diff-viewer.tsx              # the core value component
│       ├── trend-chart.tsx              # recharts line/area
│       ├── usage-meter.tsx              # progress bar: 47/100 runs used
│       ├── plan-badge.tsx               # "Free" / "Team" pill
│       └── upgrade-banner.tsx           # shown when near/at limit
│
└── tests/
    └── api/
        ├── results.test.ts              # vitest
        ├── billing.test.ts              # Stripe webhook tests
        └── security.test.ts             # rate limit, auth, injection tests
```

### Key Dependencies

```json
{
  "dependencies": {
    "next": "^14.2",
    "@supabase/supabase-js": "^2.45",
    "@supabase/ssr": "^0.5",
    "stripe": "^17",
    "@stripe/stripe-js": "^4",
    "recharts": "^2.12",
    "date-fns": "^3",
    "zod": "^3.23",
    "diff": "^7",
    "dompurify": "^3"
  },
  "devDependencies": {
    "typescript": "^5.5",
    "tailwindcss": "^3.4",
    "vitest": "^2",
    "@testing-library/react": "^16"
  }
}
```

---

## Dashboard Pages

### 1. Home (`/`)
- Lists all projects for the user's org(s)
- Each project card shows: name, last run status badge, last run timestamp, pass rate
- "New Project" button → `/new-project`

### 2. New Project (`/new-project`)
- Form: project name, org selector (if multiple orgs)
- On submit: creates project, generates first API token, shows it ONCE
- Shows: copy-paste instructions for CLI setup

### 3. Project Overview (`/[org]/[project]`)
- Header: project name, latest run status (big badge), pass rate sparkline
- Table: all tests from latest run, each row = test name, status badge, score delta, tool changes
- Quick stats: total runs, regressions this week, avg cost, avg latency
- Links to: runs, trends, tokens

### 4. Run History (`/[org]/[project]/runs`)
- Paginated table, newest first
- Columns: status badge, run ID, source (cli/ci), git branch, git SHA (linked), tests passed/total, cost, latency, timestamp
- Click row → run detail

### 5. Run Detail (`/[org]/[project]/runs/[runId]`)
- Summary bar: status, total/passed/failed counts, cost, latency, git context
- Test diff list: expandable rows, one per test
- **Diff viewer** (the core component):
  - Status badge per test
  - Score delta (green/red arrow)
  - Tool changes: list of added/removed/changed tools
  - Output similarity percentage
  - "Show raw JSON" toggle → renders `diff_json` blob
- Shareable URL (this is the link you paste in Slack or a PR)

### 6. Trends (`/[org]/[project]/trends`)
- Recharts line chart: pass rate over time (last 30 days)
- Recharts line chart: avg cost per run
- Recharts line chart: avg latency per run
- Recharts bar chart: regressions per week
- Data source: `SELECT date_trunc('day', created_at), ... FROM runs WHERE project_id = ? GROUP BY 1`

### 7. API Tokens (`/[org]/[project]/tokens`)
- Table: token name, prefix (`ev_abc1...`), created date, last used, revoke button
- "Create Token" button → name input → shows full token ONCE

### 8. Billing (`/[org]/billing`)
- Current plan badge (Free / Team)
- Usage meter: "47 / 100 runs used this month" with progress bar
- If free: upgrade CTA with plan comparison table, monthly/annual toggle
- If team: "Manage Subscription" button → Stripe Customer Portal (invoices, cancel, update payment)
- Usage history: runs per month (last 6 months) — simple bar chart
- Upgrade banner appears throughout dashboard when usage > 80% of limit

---

## CLI Changes (v1)

Minimal. 3 files changed, 1 new file.

### New: `evalview/cloud/push.py`

```python
"""Best-effort result push to EvalView Cloud.

NOT truly fire-and-forget: push_result() blocks the CLI synchronously
via asyncio.run() for up to ~15s (3 retries x 15s timeout, with backoff).
This is acceptable because:
  - Successful pushes complete in <1s (single POST)
  - Retries only trigger on transient failures (rare)
  - The CLI has already displayed all results before push runs
  - Auth/billing errors fail immediately (no retry)

If this becomes a UX problem, move to a background thread or subprocess.
"""

import asyncio
import hashlib
import logging
import os
import subprocess
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

CLOUD_API_URL = os.environ.get(
    "EVALVIEW_CLOUD_URL", "https://evalview.com/api/v1"
)


def _get_api_token() -> Optional[str]:
    """Resolve API token from env or config."""
    token = os.environ.get("EVALVIEW_API_TOKEN")
    if token:
        return token
    # Fall back to .evalview/config.yaml cloud.api_token
    try:
        from evalview.core.config import load_config
        config = load_config()
        return getattr(getattr(config, "cloud", None), "api_token", None)
    except Exception:
        return None


def _get_git_context() -> Dict[str, Any]:
    """Best-effort git metadata. Never fails."""
    ctx: Dict[str, Any] = {}
    try:
        ctx["git_sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()[:40]
    except Exception:
        pass
    try:
        ctx["git_branch"] = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        pass
    # CI PR number detection
    for env_var in ("GITHUB_PR_NUMBER", "CI_MERGE_REQUEST_IID", "PULL_REQUEST_NUMBER"):
        val = os.environ.get(env_var)
        if val:
            try:
                ctx["git_pr"] = int(val)
            except ValueError:
                pass
            break
    return ctx


async def _push_async(payload: Dict[str, Any], token: str) -> Optional[str]:
    """Push with 3 retries, exponential backoff. Returns dashboard URL or None."""
    url = f"{CLOUD_API_URL}/results"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 201:
                    return resp.json().get("dashboard_url")
                if resp.status_code in (401, 403):
                    logger.debug("Cloud push auth failed: %s", resp.status_code)
                    return None  # don't retry auth errors
                logger.debug("Cloud push failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.debug("Cloud push error (attempt %d): %s", attempt + 1, e)
        if attempt < 2:
            await asyncio.sleep(2 ** attempt)
    return None


def push_result(gate_result: Any) -> Optional[str]:
    """Push a GateResult to cloud. Best-effort, blocking.

    Runs synchronously after check results are displayed.
    Returns dashboard URL if successful, None otherwise.
    Never raises. Blocks for at most ~15s (3 retries with backoff).
    Typical latency: <1s on success, 0ms on auth failure.
    """
    token = _get_api_token()
    if not token:
        return None

    try:
        git = _get_git_context()
        source = "ci" if os.environ.get("CI") else "cli"

        payload = {
            "run_id": gate_result.raw_json.get("run_id", hashlib.md5(
                str(gate_result.raw_json).encode()
            ).hexdigest()[:8]),
            "status": gate_result.status.value,
            "source": source,
            **git,
            "summary": {
                "total": gate_result.summary.total,
                "unchanged": gate_result.summary.unchanged,
                "regressions": gate_result.summary.regressions,
                "tools_changed": gate_result.summary.tools_changed,
                "output_changed": gate_result.summary.output_changed,
            },
            "diffs": [
                {
                    "test_name": d.test_name,
                    "status": d.status.value,
                    "score_delta": d.score_delta,
                    "output_similarity": d.output_similarity,
                    "tool_changes": d.tool_changes,
                    "model_changed": d.model_changed,
                }
                for d in gate_result.diffs
            ],
            "result_json": gate_result.raw_json,
        }

        return asyncio.run(_push_async(payload, token))
    except Exception as e:
        logger.debug("Cloud push failed: %s", e)
        return None
```

### Modified: `evalview/commands/check_cmd.py`

Add `--cloud` flag and post-check push:

```python
# At end of check command, after results are displayed:

if cloud or os.environ.get("EVALVIEW_CLOUD", "").lower() in ("true", "1", "yes"):
    from evalview.cloud.push import push_result
    url = push_result(gate_result)
    if url:
        console.print(f"[dim]Results: {url}[/dim]")
```

### Modified: `evalview/commands/cloud_cmd.py`

Add `cloud connect` command (replaces the 5-step onboarding):

```python
@main.command("connect")
@click.option("--token", help="API token (or set EVALVIEW_API_TOKEN env var)")
def connect(token: str) -> None:
    """Connect this project to EvalView Cloud.

    Saves the API token to .evalview/config.yaml so subsequent
    `evalview check --cloud` calls push results automatically.
    """
    api_token = token or click.prompt("Paste your API token (from evalview.com)")
    # Save to config
    _save_cloud_config(api_token)
    console.print("[green]Connected. Run 'evalview check --cloud' to push results.[/green]")
```

### Modified: `evalview/core/config.py`

Add `CloudConfig` to the config model:

```python
class CloudConfig(BaseModel):
    api_token: Optional[str] = None
    auto_push: bool = False

class EvalViewConfig(BaseModel):
    # ... existing fields ...
    cloud: Optional[CloudConfig] = None
```

---

## Deployment

### Vercel

```json
// cloud/vercel.json
{
  "framework": "nextjs",
  "regions": ["iad1"]
}
```

Environment variables in Vercel dashboard:

| Variable | Visibility | Notes |
|----------|-----------|-------|
| `SUPABASE_URL` | Server-only | Same value as NEXT_PUBLIC, but server-only copy for admin client |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only, encrypted | **CRITICAL** — full DB access. Never prefix with `NEXT_PUBLIC_`. |
| `NEXT_PUBLIC_SUPABASE_URL` | Public | Safe — Supabase URL is not a secret |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Public | Safe — anon key is rate-limited and RLS-gated |
| `STRIPE_SECRET_KEY` | Server-only, encrypted | Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | Server-only, encrypted | For webhook signature verification |
| `STRIPE_PRICE_TEAM_MONTHLY` | Server-only | Stripe price ID for monthly plan |
| `STRIPE_PRICE_TEAM_ANNUAL` | Server-only | Stripe price ID for annual plan |
| `NEXT_PUBLIC_APP_URL` | Public | `https://evalview.com` — used for Stripe redirect URLs |

**Security note:** Disable "Automatically expose env vars to Preview Deployments" in Vercel for all server-only variables. PR preview deployments should use a separate Supabase project with test data.

### Supabase

- Region: US East (co-located with Vercel)
- Plan: Free tier for alpha, Pro ($25/mo) for production (needed for pg_cron, more connections)
- Enable: GitHub OAuth in Auth settings
- Auth settings: set `Site URL` to `https://evalview.com`, add `https://evalview.com/callback` to redirect whitelist
- Enable RLS on all tables (enforced by migration 00002)
- Database: encrypted at rest (Supabase default, AES-256)
- Network: SSL enforced on all connections (Supabase default)

### Domain

- `evalview.com` → Vercel (single domain, app + marketing)
- No separate `api.evalview.com` needed — Next.js handles `/api/v1/*` routes

---

## 6-Week Implementation Order

### Week 1: Foundation + Security Baseline

- [ ] `npx create-next-app cloud --typescript --tailwind --app`
- [ ] Supabase project setup, run migrations 00001 + 00002 + 00003
- [ ] Supabase Auth config (GitHub OAuth provider, redirect whitelist)
- [ ] Auth flow: login page, callback handler, auto-provisioning (personal org on first login)
- [ ] `createAdminClient()` and `createServerSupabase()` helpers
- [ ] `middleware.ts`: security headers (CSP, HSTS, X-Frame-Options, CORS)
- [ ] `verifyApiToken()` with SHA-256 hash lookup
- [ ] `generateApiToken()` with 256-bit random + hash storage
- [ ] `rate-limit.ts` calling `check_rate_limit()` DB function
- [ ] `audit.ts` logging to `audit_log` table
- [ ] Deploy to Vercel (empty shell, auth works, headers verified)

**Exit criteria:** Login works, security headers pass [securityheaders.com](https://securityheaders.com) scan, API token generation returns properly hashed tokens.

### Week 2: API + CLI + Security Tests

- [ ] `POST /api/v1/results` (Zod validation, rate limit, usage check, dedup, audit)
- [ ] Dashboard data layer: server-component queries for runs/diffs via session client + RLS
- [ ] `POST /api/v1/projects` (create project + first token, audit logged)
- [ ] `POST /api/v1/tokens` + `DELETE /api/v1/tokens/[id]` (audit logged)
- [ ] CLI: `evalview/cloud/push.py` (best-effort post-check push)
- [ ] CLI: `--cloud` flag on `check` command
- [ ] CLI: `evalview connect` command
- [ ] `next.config.ts`: body size limit (1MB), powered-by header disabled
- [ ] API tests (vitest): happy path, auth failures, rate limit, invalid payloads
- [ ] Security tests: injection attempts, oversized payloads, expired tokens, revoked tokens
- [ ] Idempotency test: push same run_id twice → usage increments once, test_diffs not duplicated
- [ ] CSRF test: cross-origin POST to session-authed routes → 403

**Exit criteria:** `evalview check --cloud` pushes results. Dashboard shows them. Duplicate pushes are idempotent. Unauthorized/invalid/oversized/cross-origin requests are rejected with proper status codes.

### Week 3: Dashboard

- [ ] Dashboard layout: sidebar, org switcher, plan badge
- [ ] Home page: project list with status cards
- [ ] New project page: form → API token (show once, warn about single display)
- [ ] Project overview: test list from latest run
- [ ] Run history: paginated table
- [ ] Run detail: summary bar + test diff list
- [ ] Diff viewer component (the core value — port from visualization/generators.py)
- [ ] DOMPurify on any raw JSON / diff rendering (prevent stored XSS via `diff_json`)
- [ ] Status badges component
- [ ] API tokens page: list + create + revoke
- [ ] Upgrade banner component (shown when usage > 80%)

**Exit criteria:** Full dashboard navigation works. Diff viewer renders tool changes and output diffs. Raw JSON display is sanitized.

### Week 4: Billing

- [ ] Stripe product + prices setup (Team monthly, Team annual)
- [ ] `POST /api/v1/billing/checkout` — creates Stripe Checkout session
- [ ] `POST /api/v1/billing/portal` — redirects to Stripe Customer Portal
- [ ] `POST /api/v1/billing/webhook` — signature-verified handler for subscription lifecycle
- [ ] `GET /api/v1/billing/usage` — returns current period usage for org
- [ ] `checkUsage()` + `incrementUsage()` integrated into `POST /results` (only count new inserts, not retries)
- [ ] Billing page: plan display, usage meter, upgrade CTA, manage subscription
- [ ] Usage meter component with progress bar
- [ ] 402 response from CLI when limit exceeded (clear upgrade message)
- [ ] Downgrade logic: archive excess projects, cap at free limits
- [ ] Stripe webhook tests (vitest: mock signature, test all event types)

**Exit criteria:** Full billing loop works: sign up free → hit limit → upgrade → Stripe Checkout → webhook confirms → plan changes → limits raised. Downgrade works via Customer Portal.

### Week 5: Trends + Polish

- [ ] Trends page: pass rate, cost, latency charts (Recharts)
- [ ] Usage history chart on billing page
- [ ] Error states: empty projects, no runs yet, token expired, usage exceeded
- [ ] Mobile-responsive sidebar
- [ ] `action.yml` update: add `cloud-token` input, push step
- [ ] Data retention enforcement: scheduled cleanup of expired runs (7-day free, 90-day team)
- [ ] Rate limit cleanup: scheduled daily purge of old `rate_limits` rows
- [ ] Audit log cleanup: scheduled 90-day purge

**Exit criteria:** Dashboard is complete and polished. Retention is enforced. Background cleanup jobs run.

### Week 6: Hardening + Launch

- [ ] Penetration testing: OWASP top 10 checklist against all API routes
- [ ] Test: what happens when Supabase is down? (rate limit fail-open, push retries)
- [ ] Test: what happens when Stripe is down? (checkout fails gracefully, webhook retries)
- [ ] Test: token brute-force attempt (verify rate limiter blocks after 60/min)
- [ ] Test: oversized payloads, malformed JSON, SQL injection strings in test_name
- [ ] Verify: `SUPABASE_SERVICE_ROLE_KEY` not in any client-side JS bundle
- [ ] Verify: Stripe webhook secret not logged anywhere
- [ ] README update with cloud quickstart
- [ ] Dogfood: use it on eval-view itself for 3+ days
- [ ] Set up Sentry for error tracking
- [ ] Deploy production, announce

**Exit criteria:** Security checklist passes. Dogfooding is stable. Production deployment is live.

---

## What Comes After v1

Once v1 is live and being used, evaluate in this order:

1. **Read-only golden metadata view** — show what goldens exist per test, without sync. Low effort, helps understand the project.
2. **CI PR comments from cloud** — GitHub App, webhook receiver, post comments as EvalView bot. This is the stickiest paid feature.
3. **Bidirectional golden sync** — only after you have a conflict resolution strategy.
4. **Hosted judge** — proxy LLM calls through cloud for users without API keys. Metered via usage table.
5. **Realtime** — Supabase Realtime on `runs` table for live dashboard updates.
6. **Enterprise tier** — SSO (SAML), audit log export, custom retention, dedicated support.
7. **SOC 2 readiness** — audit log retention, access controls, incident response SOP. Start the process when enterprise deals require it.

Each of these is a self-contained follow-up plan, not part of v1.

---

## Security Checklist (Pre-Launch Gate)

Do not launch until every item is verified:

- [ ] All API routes validate input with Zod (no raw `req.body` usage)
- [ ] All API routes check Content-Length < 1MB before parsing body
- [ ] All API-token routes check rate limits
- [ ] All API-token routes check usage limits (free tier enforced)
- [ ] Usage increments only on new run inserts (duplicate push = no increment)
- [ ] Test diffs are delete-then-insert on duplicate push (no row duplication)
- [ ] All session-authenticated POST/DELETE routes call `verifyCsrf()` (Origin/Referer check)
- [ ] All session-authenticated mutations verify org membership/admin role
- [ ] Stripe webhook handler verifies HMAC signature
- [ ] `SUPABASE_SERVICE_ROLE_KEY` confirmed absent from client JS bundle (check Vercel build output)
- [ ] `STRIPE_SECRET_KEY` confirmed absent from client JS bundle
- [ ] Security headers pass [securityheaders.com](https://securityheaders.com) A rating
- [ ] Production CSP has NO `unsafe-eval` — verify in deployed response headers
- [ ] CORS only allows `evalview.com` origin
- [ ] API tokens are 256-bit random, stored as SHA-256 hash only
- [ ] Raw tokens shown to user exactly once, never logged, never stored
- [ ] `diff_json` and `result_json` sanitized with DOMPurify before rendering
- [ ] Audit log captures: login failures, token creation/revocation, plan changes, rate limit breaches
- [ ] Preview deployments do NOT have production env vars
- [ ] No `console.log` of tokens, keys, or session data anywhere in codebase
- [ ] `next.config.ts` sets `poweredBy: false`
- [ ] Supabase RLS enabled on all tables, verified with test queries as anon role
- [ ] Data retention cleanup runs on schedule (sets `runs.deleted_at` for expired runs, hard-deletes after 30 days)
- [ ] Downgrade sets `projects.status = 'archived'` on excess projects, blocks new pushes
- [ ] Dashboard URLs resolve correctly with org/project slugs (not raw UUIDs)
- [ ] Stripe Customer Portal enabled for self-serve cancel/update
