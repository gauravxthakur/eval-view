# EvalView Cloud — Technical Implementation Plan

**Stack: Next.js 14 (App Router) · TypeScript · Supabase (Auth, Postgres, Storage, Edge Functions) · Stripe**
*Based on codebase analysis of eval-view v0.6.1 · March 2026*

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Repository Structure](#2-repository-structure)
3. [Supabase Schema & Migrations](#3-supabase-schema--migrations)
4. [Row-Level Security (RLS)](#4-row-level-security-rls)
5. [Authentication Flow](#5-authentication-flow)
6. [API Layer — Next.js Route Handlers](#6-api-layer--nextjs-route-handlers)
7. [CLI-to-Cloud Integration](#7-cli-to-cloud-integration)
8. [Dashboard Pages & Components](#8-dashboard-pages--components)
9. [CI/CD Webhook System](#9-cicd-webhook-system)
10. [Billing (Stripe)](#10-billing-stripe)
11. [Hosted Judge (v1.1)](#11-hosted-judge-v11)
12. [CLI Changes Required](#12-cli-changes-required)
13. [Deployment & Infrastructure](#13-deployment--infrastructure)
14. [Phased Delivery Plan](#14-phased-delivery-plan)
15. [Open Questions & Decisions](#15-open-questions--decisions)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                        User's Machine / CI                       │
│                                                                  │
│  evalview check ──► local execution ──► GateResult JSON          │
│       │                                      │                   │
│       │  (agent stays local,                 │  async POST       │
│       │   API keys stay local)               │  fire-and-forget  │
│       ▼                                      ▼                   │
│  evalview login ◄──────────────── EVALVIEW_CLOUD=true            │
└──────────────────────────────┬───────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│                      EvalView Cloud (Vercel)                     │
│                                                                  │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────────────┐     │
│  │  Next.js     │   │  API Routes  │   │  Webhook Handler  │     │
│  │  Dashboard   │   │  /api/v1/*   │   │  /api/webhook/gh  │     │
│  │  (App Router)│   │              │   │                   │     │
│  └──────┬──────┘   └──────┬───────┘   └────────┬──────────┘     │
│         │                 │                     │                │
│         ▼                 ▼                     ▼                │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Supabase                               │   │
│  │  ┌──────┐  ┌──────────┐  ┌─────────┐  ┌──────────────┐  │   │
│  │  │ Auth │  │ Postgres │  │ Storage │  │Edge Functions│  │   │
│  │  │GitHub│  │  (RLS)   │  │(goldens)│  │(hosted judge)│  │   │
│  │  │Google│  │          │  │         │  │              │  │   │
│  │  └──────┘  └──────────┘  └─────────┘  └──────────────┘  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Stripe                                 │   │
│  │  Subscriptions · Usage metering · Customer portal         │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### Core Principles

1. **Execution stays local.** The cloud never calls user agents or holds API keys.
2. **Results flow up.** CLI/CI pushes `GateResult` JSON after local execution.
3. **Goldens sync bidirectionally.** Cloud is the source of truth for shared baselines.
4. **Offline-safe.** Cloud push is async with retry. CLI never blocks on cloud availability.
5. **Multi-tenant from day one.** Organizations → Projects → Members, all RLS-scoped.

---

## 2. Repository Structure

New `cloud/` directory at repo root (separate from `evalview/cloud/` which is the CLI-side client):

```
cloud/                              # NEW — Next.js app
├── package.json
├── tsconfig.json
├── next.config.ts
├── tailwind.config.ts
├── .env.local.example              # SUPABASE_URL, SUPABASE_ANON_KEY, etc.
│
├── supabase/
│   ├── config.toml                 # Supabase project config
│   ├── migrations/
│   │   ├── 00001_initial_schema.sql
│   │   ├── 00002_rls_policies.sql
│   │   ├── 00003_api_tokens.sql
│   │   ├── 00004_billing.sql
│   │   └── 00005_usage_tracking.sql
│   ├── functions/
│   │   ├── hosted-judge/           # Edge Function (v1.1)
│   │   │   └── index.ts
│   │   └── webhook-github/         # Edge Function for GH webhooks
│   │       └── index.ts
│   └── seed.sql
│
├── src/
│   ├── app/
│   │   ├── layout.tsx              # Root layout (auth provider, sidebar)
│   │   ├── page.tsx                # Landing / marketing (evalview.com)
│   │   ├── (auth)/
│   │   │   ├── login/page.tsx
│   │   │   └── callback/page.tsx   # OAuth callback handler
│   │   ├── (dashboard)/
│   │   │   ├── layout.tsx          # Authenticated layout (sidebar, org switcher)
│   │   │   ├── page.tsx            # Dashboard home (project list)
│   │   │   ├── [orgSlug]/
│   │   │   │   ├── page.tsx        # Org overview
│   │   │   │   ├── settings/page.tsx
│   │   │   │   ├── members/page.tsx
│   │   │   │   └── billing/page.tsx
│   │   │   └── [orgSlug]/[projectSlug]/
│   │   │       ├── page.tsx        # Project dashboard (test list + status)
│   │   │       ├── tests/
│   │   │       │   └── [testName]/page.tsx  # Test detail (diff viewer)
│   │   │       ├── runs/
│   │   │       │   ├── page.tsx    # Run history
│   │   │       │   └── [runId]/page.tsx     # Single run detail
│   │   │       ├── trends/page.tsx # Score/cost/latency charts
│   │   │       ├── settings/page.tsx
│   │   │       └── tokens/page.tsx # API token management
│   │   └── api/
│   │       └── v1/
│   │           ├── results/route.ts        # POST results from CLI/CI
│   │           ├── results/[runId]/route.ts # GET single result
│   │           ├── goldens/route.ts         # GET/PUT golden sync
│   │           ├── goldens/[testName]/route.ts
│   │           ├── projects/route.ts        # CRUD projects
│   │           ├── projects/[id]/route.ts
│   │           ├── tokens/route.ts          # API token management
│   │           ├── webhook/
│   │           │   └── github/route.ts      # GitHub webhook receiver
│   │           ├── billing/
│   │           │   ├── checkout/route.ts    # Create Stripe checkout
│   │           │   └── webhook/route.ts     # Stripe webhook
│   │           └── judge/route.ts           # Hosted judge proxy (v1.1)
│   │
│   ├── lib/
│   │   ├── supabase/
│   │   │   ├── client.ts           # Browser Supabase client
│   │   │   ├── server.ts           # Server-side Supabase client
│   │   │   ├── middleware.ts        # Auth middleware for Next.js
│   │   │   └── admin.ts            # Service-role client (webhooks, billing)
│   │   ├── stripe/
│   │   │   ├── client.ts           # Stripe SDK init
│   │   │   ├── plans.ts            # Plan definitions & limits
│   │   │   └── usage.ts            # Usage metering helpers
│   │   ├── api-auth.ts             # API token verification for CLI/CI
│   │   ├── rate-limit.ts           # Token bucket rate limiter
│   │   ├── webhook-verify.ts       # HMAC signature verification
│   │   └── types.ts                # Shared TypeScript types
│   │
│   ├── components/
│   │   ├── ui/                     # shadcn/ui primitives
│   │   ├── dashboard/
│   │   │   ├── project-card.tsx
│   │   │   ├── test-status-badge.tsx
│   │   │   ├── run-table.tsx
│   │   │   ├── diff-viewer.tsx     # Side-by-side golden diff
│   │   │   ├── trend-chart.tsx     # Recharts score/cost/latency
│   │   │   ├── org-switcher.tsx
│   │   │   └── sidebar.tsx
│   │   └── billing/
│   │       ├── plan-card.tsx
│   │       ├── usage-meter.tsx
│   │       └── upgrade-banner.tsx
│   │
│   └── hooks/
│       ├── use-project.ts
│       ├── use-runs.ts
│       └── use-realtime.ts         # Supabase Realtime subscriptions
│
├── public/
│   └── ...
└── tests/
    ├── api/                        # API route tests (vitest)
    └── components/                 # Component tests (vitest + testing-library)
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
    "diff": "^7"
  },
  "devDependencies": {
    "typescript": "^5.5",
    "tailwindcss": "^3.4",
    "@shadcn/ui": "latest",
    "vitest": "^2",
    "@testing-library/react": "^16"
  }
}
```

---

## 3. Supabase Schema & Migrations

### Migration 00001: Core Schema

```sql
-- 00001_initial_schema.sql

-- Organizations (teams)
CREATE TABLE organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    slug        TEXT UNIQUE NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free',  -- free, team, enterprise
    stripe_customer_id   TEXT UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_orgs_slug ON organizations(slug);

-- Organization members
CREATE TABLE org_members (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'member',  -- owner, admin, member
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, user_id)
);

CREATE INDEX idx_org_members_user ON org_members(user_id);
CREATE INDEX idx_org_members_org ON org_members(org_id);

-- Projects
CREATE TABLE projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL,
    adapter     TEXT,                            -- http, langgraph, crewai, etc.
    endpoint    TEXT,                            -- agent endpoint (display only)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, slug)
);

CREATE INDEX idx_projects_org ON projects(org_id);

-- Test runs (maps to GateResult from CLI)
CREATE TABLE runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id      TEXT NOT NULL,                   -- CLI-generated run ID
    status      TEXT NOT NULL,                   -- passed, regression, tools_changed, output_changed
    source      TEXT NOT NULL DEFAULT 'cli',     -- cli, ci, sdk, monitor
    git_sha     TEXT,
    git_branch  TEXT,
    git_pr      INTEGER,                         -- PR number if from CI
    total_tests INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    regressions INTEGER NOT NULL DEFAULT 0,
    tools_changed INTEGER NOT NULL DEFAULT 0,
    output_changed INTEGER NOT NULL DEFAULT 0,
    total_cost  REAL DEFAULT 0.0,
    total_latency_ms REAL DEFAULT 0.0,
    result_json JSONB NOT NULL,                  -- Full GateResult payload
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  UUID REFERENCES auth.users(id)
);

CREATE INDEX idx_runs_project ON runs(project_id);
CREATE INDEX idx_runs_created ON runs(created_at DESC);
CREATE INDEX idx_runs_status ON runs(status);
CREATE INDEX idx_runs_git_pr ON runs(git_pr) WHERE git_pr IS NOT NULL;
CREATE INDEX idx_runs_project_created ON runs(project_id, created_at DESC);

-- Per-test diffs within a run
CREATE TABLE test_diffs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    test_name       TEXT NOT NULL,
    status          TEXT NOT NULL,                -- passed, regression, tools_changed, output_changed
    score_delta     REAL DEFAULT 0.0,
    output_similarity REAL,
    semantic_similarity REAL,
    tool_changes    INTEGER DEFAULT 0,
    model_changed   BOOLEAN DEFAULT FALSE,
    diff_json       JSONB,                       -- Full TraceDiff for this test
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_test_diffs_run ON test_diffs(run_id);
CREATE INDEX idx_test_diffs_test_name ON test_diffs(test_name);
CREATE INDEX idx_test_diffs_status ON test_diffs(status);

-- Golden baselines metadata (files in Supabase Storage, metadata here)
CREATE TABLE goldens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    test_name   TEXT NOT NULL,
    variant     TEXT DEFAULT 'default',           -- default, v1, v2, etc.
    storage_path TEXT NOT NULL,                   -- path in Supabase Storage bucket
    checksum    TEXT NOT NULL,                    -- SHA-256 of golden JSON
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  UUID REFERENCES auth.users(id),
    UNIQUE(project_id, test_name, variant)
);

CREATE INDEX idx_goldens_project ON goldens(project_id);

-- Updated-at trigger
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at_orgs
    BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER set_updated_at_projects
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

### Migration 00002: API Tokens

```sql
-- 00002_api_tokens.sql

CREATE TABLE api_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                    -- "CI token", "staging", etc.
    token_hash  TEXT UNIQUE NOT NULL,             -- SHA-256 of the token (never store raw)
    token_prefix TEXT NOT NULL,                   -- First 8 chars for identification: "ev_abc123..."
    scopes      TEXT[] NOT NULL DEFAULT '{write:results,read:goldens,write:goldens}',
    last_used_at TIMESTAMPTZ,
    expires_at  TIMESTAMPTZ,                     -- NULL = never expires
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by  UUID NOT NULL REFERENCES auth.users(id),
    revoked_at  TIMESTAMPTZ                      -- soft-delete
);

CREATE INDEX idx_api_tokens_hash ON api_tokens(token_hash) WHERE revoked_at IS NULL;
CREATE INDEX idx_api_tokens_project ON api_tokens(project_id);

-- Function to verify token and return project context
CREATE OR REPLACE FUNCTION verify_api_token(p_token_hash TEXT)
RETURNS TABLE(
    project_id UUID,
    org_id UUID,
    scopes TEXT[],
    plan TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        t.project_id,
        p.org_id,
        t.scopes,
        o.plan
    FROM api_tokens t
    JOIN projects p ON t.project_id = p.id
    JOIN organizations o ON p.org_id = o.id
    WHERE t.token_hash = p_token_hash
      AND t.revoked_at IS NULL
      AND (t.expires_at IS NULL OR t.expires_at > now());

    -- Update last_used_at (fire-and-forget)
    UPDATE api_tokens SET last_used_at = now()
    WHERE token_hash = p_token_hash;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
```

### Migration 00003: Usage Tracking & Billing

```sql
-- 00003_usage_billing.sql

-- Usage tracking (per billing period)
CREATE TABLE usage (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    period      TEXT NOT NULL,                    -- "2026-03" (YYYY-MM)
    test_runs   INTEGER NOT NULL DEFAULT 0,
    judge_evals INTEGER NOT NULL DEFAULT 0,
    storage_bytes BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(org_id, period)
);

CREATE INDEX idx_usage_org_period ON usage(org_id, period);

-- Increment usage atomically
CREATE OR REPLACE FUNCTION increment_usage(
    p_org_id UUID,
    p_test_runs INTEGER DEFAULT 0,
    p_judge_evals INTEGER DEFAULT 0,
    p_storage_bytes BIGINT DEFAULT 0
) RETURNS void AS $$
BEGIN
    INSERT INTO usage (org_id, period, test_runs, judge_evals, storage_bytes)
    VALUES (p_org_id, to_char(now(), 'YYYY-MM'), p_test_runs, p_judge_evals, p_storage_bytes)
    ON CONFLICT (org_id, period)
    DO UPDATE SET
        test_runs = usage.test_runs + p_test_runs,
        judge_evals = usage.judge_evals + p_judge_evals,
        storage_bytes = GREATEST(usage.storage_bytes, p_storage_bytes),
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Plan limits (queryable from app)
CREATE TABLE plan_limits (
    plan             TEXT PRIMARY KEY,
    max_test_runs    INTEGER NOT NULL,
    max_projects     INTEGER NOT NULL,
    max_members      INTEGER NOT NULL,
    max_storage_mb   INTEGER NOT NULL,
    max_judge_evals  INTEGER NOT NULL,
    retention_days   INTEGER NOT NULL,
    ci_providers     TEXT[] NOT NULL,
    pr_comments      BOOLEAN NOT NULL DEFAULT FALSE,
    slack_alerts     BOOLEAN NOT NULL DEFAULT FALSE
);

INSERT INTO plan_limits VALUES
    ('free',       100,  2,  1,  100,   50,   7, '{github}',               FALSE, FALSE),
    ('team',      10000, -1, 10, 5120, 2000,  90, '{github,gitlab,webhook}', TRUE,  TRUE),
    ('enterprise', -1,   -1, -1,   -1,   -1, 365, '{github,gitlab,webhook}', TRUE,  TRUE);
-- -1 means unlimited
```

### Migration 00004: Rate Limiting

```sql
-- 00004_rate_limiting.sql

-- Rate limit tracking (sliding window, per API token)
CREATE TABLE rate_limits (
    token_hash  TEXT NOT NULL,
    window      TIMESTAMPTZ NOT NULL,            -- Truncated to minute
    count       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (token_hash, window)
);

-- Auto-cleanup old windows (run via pg_cron daily)
CREATE OR REPLACE FUNCTION cleanup_rate_limits()
RETURNS void AS $$
BEGIN
    DELETE FROM rate_limits WHERE window < now() - INTERVAL '1 hour';
END;
$$ LANGUAGE plpgsql;

-- Check and increment rate limit (returns TRUE if allowed)
CREATE OR REPLACE FUNCTION check_rate_limit(
    p_token_hash TEXT,
    p_max_per_minute INTEGER DEFAULT 60
) RETURNS BOOLEAN AS $$
DECLARE
    current_window TIMESTAMPTZ;
    current_count INTEGER;
BEGIN
    current_window := date_trunc('minute', now());

    INSERT INTO rate_limits (token_hash, window, count)
    VALUES (p_token_hash, current_window, 1)
    ON CONFLICT (token_hash, window)
    DO UPDATE SET count = rate_limits.count + 1
    RETURNING count INTO current_count;

    RETURN current_count <= p_max_per_minute;
END;
$$ LANGUAGE plpgsql;
```

---

## 4. Row-Level Security (RLS)

```sql
-- 00002_rls_policies.sql

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE test_diffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE goldens ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage ENABLE ROW LEVEL SECURITY;

-- Helper: check if user is member of org
CREATE OR REPLACE FUNCTION is_org_member(p_org_id UUID)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM org_members
        WHERE org_id = p_org_id AND user_id = auth.uid()
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Helper: check if user is owner/admin of org
CREATE OR REPLACE FUNCTION is_org_admin(p_org_id UUID)
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM org_members
        WHERE org_id = p_org_id
          AND user_id = auth.uid()
          AND role IN ('owner', 'admin')
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Organizations: members can read, admins can update
CREATE POLICY orgs_select ON organizations
    FOR SELECT USING (is_org_member(id));

CREATE POLICY orgs_insert ON organizations
    FOR INSERT WITH CHECK (TRUE);  -- anyone can create an org

CREATE POLICY orgs_update ON organizations
    FOR UPDATE USING (is_org_admin(id));

-- Org members: members can read their own org, admins can insert/delete
CREATE POLICY members_select ON org_members
    FOR SELECT USING (is_org_member(org_id));

CREATE POLICY members_insert ON org_members
    FOR INSERT WITH CHECK (is_org_admin(org_id) OR user_id = auth.uid());

CREATE POLICY members_delete ON org_members
    FOR DELETE USING (is_org_admin(org_id));

-- Projects: org members can read, admins can write
CREATE POLICY projects_select ON projects
    FOR SELECT USING (is_org_member(org_id));

CREATE POLICY projects_insert ON projects
    FOR INSERT WITH CHECK (is_org_member(org_id));

CREATE POLICY projects_update ON projects
    FOR UPDATE USING (is_org_admin(org_id));

CREATE POLICY projects_delete ON projects
    FOR DELETE USING (is_org_admin(org_id));

-- Runs: org members can read, any member can insert (from CLI/CI)
CREATE POLICY runs_select ON runs
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM projects p
            WHERE p.id = runs.project_id AND is_org_member(p.org_id)
        )
    );

CREATE POLICY runs_insert ON runs
    FOR INSERT WITH CHECK (
        EXISTS (
            SELECT 1 FROM projects p
            WHERE p.id = runs.project_id AND is_org_member(p.org_id)
        )
    );

-- Test diffs: inherit from runs
CREATE POLICY test_diffs_select ON test_diffs
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM runs r
            JOIN projects p ON r.project_id = p.id
            WHERE r.id = test_diffs.run_id AND is_org_member(p.org_id)
        )
    );

CREATE POLICY test_diffs_insert ON test_diffs
    FOR INSERT WITH CHECK (
        EXISTS (
            SELECT 1 FROM runs r
            JOIN projects p ON r.project_id = p.id
            WHERE r.id = test_diffs.run_id AND is_org_member(p.org_id)
        )
    );

-- Goldens: org members can read/write
CREATE POLICY goldens_all ON goldens
    FOR ALL USING (
        EXISTS (
            SELECT 1 FROM projects p
            WHERE p.id = goldens.project_id AND is_org_member(p.org_id)
        )
    );

-- API tokens: project members can read their own, admins can manage
CREATE POLICY tokens_select ON api_tokens
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM projects p
            WHERE p.id = api_tokens.project_id AND is_org_member(p.org_id)
        )
    );

CREATE POLICY tokens_insert ON api_tokens
    FOR INSERT WITH CHECK (
        EXISTS (
            SELECT 1 FROM projects p
            WHERE p.id = api_tokens.project_id AND is_org_admin(p.org_id)
        )
    );

-- Usage: org members can read
CREATE POLICY usage_select ON usage
    FOR SELECT USING (is_org_member(org_id));
```

---

## 5. Authentication Flow

### Browser (Dashboard)

Uses `@supabase/ssr` for cookie-based auth with Next.js middleware:

```typescript
// src/lib/supabase/middleware.ts
import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

export async function updateSession(request: NextRequest) {
  const response = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll: () => request.cookies.getAll(),
        setAll: (cookies) => {
          cookies.forEach(({ name, value, options }) => {
            response.cookies.set(name, value, options);
          });
        },
      },
    }
  );

  const { data: { user } } = await supabase.auth.getUser();

  // Protect dashboard routes
  if (!user && request.nextUrl.pathname.startsWith("/(dashboard)")) {
    return NextResponse.redirect(new URL("/login", request.url));
  }

  return response;
}
```

### CLI / CI (API Token)

API routes accept a Bearer token (project-scoped API key):

```typescript
// src/lib/api-auth.ts
import { createClient } from "@supabase/supabase-js";
import { createHash } from "crypto";

const supabaseAdmin = createClient(
  process.env.SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!
);

export interface ApiContext {
  projectId: string;
  orgId: string;
  scopes: string[];
  plan: string;
}

export async function verifyApiToken(
  authHeader: string | null
): Promise<ApiContext | null> {
  if (!authHeader?.startsWith("Bearer ev_")) return null;

  const token = authHeader.slice(7); // Remove "Bearer "
  const tokenHash = createHash("sha256").update(token).digest("hex");

  const { data, error } = await supabaseAdmin.rpc("verify_api_token", {
    p_token_hash: tokenHash,
  });

  if (error || !data?.[0]) return null;

  return {
    projectId: data[0].project_id,
    orgId: data[0].org_id,
    scopes: data[0].scopes,
    plan: data[0].plan,
  };
}
```

### First Login — Auto-Provisioning

When a user logs in for the first time (via GitHub OAuth), create a personal org:

```typescript
// src/app/(auth)/callback/page.tsx — post-OAuth hook
async function provisionNewUser(userId: string, email: string) {
  const slug = email.split("@")[0].replace(/[^a-z0-9-]/g, "-");

  // Create personal org
  const { data: org } = await supabase
    .from("organizations")
    .insert({ name: `${slug}'s org`, slug })
    .select()
    .single();

  // Add user as owner
  await supabase
    .from("org_members")
    .insert({ org_id: org.id, user_id: userId, role: "owner" });
}
```

---

## 6. API Layer — Next.js Route Handlers

### POST /api/v1/results — Push Run Results

The most important endpoint. Called by CLI and CI after local execution.

```typescript
// src/app/api/v1/results/route.ts
import { NextRequest, NextResponse } from "next/server";
import { verifyApiToken } from "@/lib/api-auth";
import { checkRateLimit } from "@/lib/rate-limit";
import { checkUsageLimits } from "@/lib/stripe/usage";
import { z } from "zod";

const ResultPayload = z.object({
  run_id: z.string(),
  status: z.enum(["passed", "regression", "tools_changed", "output_changed"]),
  source: z.enum(["cli", "ci", "sdk", "monitor"]).default("cli"),
  git_sha: z.string().optional(),
  git_branch: z.string().optional(),
  git_pr: z.number().optional(),
  summary: z.object({
    total: z.number(),
    unchanged: z.number(),
    regressions: z.number(),
    tools_changed: z.number(),
    output_changed: z.number(),
  }),
  diffs: z.array(z.object({
    test_name: z.string(),
    status: z.string(),
    score_delta: z.number().default(0),
    output_similarity: z.number().nullable().default(null),
    semantic_similarity: z.number().nullable().default(null),
    tool_changes: z.number().default(0),
    model_changed: z.boolean().default(false),
    diff_json: z.any().optional(),
  })),
  full_result: z.any(),                          // Raw GateResult JSON
});

export async function POST(req: NextRequest) {
  // 1. Auth
  const ctx = await verifyApiToken(req.headers.get("authorization"));
  if (!ctx) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  if (!ctx.scopes.includes("write:results")) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  // 2. Rate limit
  const allowed = await checkRateLimit(ctx.projectId);
  if (!allowed) {
    return NextResponse.json(
      { error: "Rate limit exceeded" },
      { status: 429, headers: { "Retry-After": "60" } }
    );
  }

  // 3. Usage limit
  const withinLimits = await checkUsageLimits(ctx.orgId, ctx.plan, "test_runs", 1);
  if (!withinLimits) {
    return NextResponse.json(
      { error: "Monthly test run limit reached. Upgrade at evalview.com/billing" },
      { status: 402 }
    );
  }

  // 4. Validate
  const body = await req.json();
  const parsed = ResultPayload.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid payload", details: parsed.error.flatten() },
      { status: 400 }
    );
  }

  const payload = parsed.data;

  // 5. Insert run
  const { data: run, error: runError } = await supabaseAdmin
    .from("runs")
    .insert({
      project_id: ctx.projectId,
      run_id: payload.run_id,
      status: payload.status,
      source: payload.source,
      git_sha: payload.git_sha,
      git_branch: payload.git_branch,
      git_pr: payload.git_pr,
      total_tests: payload.summary.total,
      unchanged: payload.summary.unchanged,
      regressions: payload.summary.regressions,
      tools_changed: payload.summary.tools_changed,
      output_changed: payload.summary.output_changed,
      result_json: payload.full_result,
    })
    .select("id")
    .single();

  if (runError) {
    return NextResponse.json({ error: "Failed to save run" }, { status: 500 });
  }

  // 6. Insert test diffs
  if (payload.diffs.length > 0) {
    const diffRows = payload.diffs.map((d) => ({
      run_id: run.id,
      test_name: d.test_name,
      status: d.status,
      score_delta: d.score_delta,
      output_similarity: d.output_similarity,
      semantic_similarity: d.semantic_similarity,
      tool_changes: d.tool_changes,
      model_changed: d.model_changed,
      diff_json: d.diff_json,
    }));

    await supabaseAdmin.from("test_diffs").insert(diffRows);
  }

  // 7. Increment usage
  await supabaseAdmin.rpc("increment_usage", {
    p_org_id: ctx.orgId,
    p_test_runs: payload.summary.total,
  });

  return NextResponse.json({
    id: run.id,
    url: `https://evalview.com/${ctx.orgId}/${ctx.projectId}/runs/${run.id}`,
  }, { status: 201 });
}
```

### GET /api/v1/goldens — List & Sync Goldens

```typescript
// src/app/api/v1/goldens/route.ts
export async function GET(req: NextRequest) {
  const ctx = await verifyApiToken(req.headers.get("authorization"));
  if (!ctx) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  const { data } = await supabaseAdmin
    .from("goldens")
    .select("test_name, variant, checksum, size_bytes, created_at")
    .eq("project_id", ctx.projectId);

  return NextResponse.json({ goldens: data });
}

export async function PUT(req: NextRequest) {
  const ctx = await verifyApiToken(req.headers.get("authorization"));
  if (!ctx) return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

  if (!ctx.scopes.includes("write:goldens")) {
    return NextResponse.json({ error: "Forbidden" }, { status: 403 });
  }

  const { test_name, variant, golden_json } = await req.json();

  // Upload to Supabase Storage
  const storagePath = `${ctx.orgId}/${ctx.projectId}/${test_name}.${variant}.golden.json`;
  const body = JSON.stringify(golden_json);
  const checksum = createHash("sha256").update(body).digest("hex");

  await supabaseAdmin.storage
    .from("goldens")
    .upload(storagePath, body, { upsert: true, contentType: "application/json" });

  // Upsert metadata
  await supabaseAdmin
    .from("goldens")
    .upsert({
      project_id: ctx.projectId,
      test_name,
      variant: variant || "default",
      storage_path: storagePath,
      checksum,
      size_bytes: Buffer.byteLength(body),
    }, { onConflict: "project_id,test_name,variant" });

  return NextResponse.json({ ok: true });
}
```

### Full API Surface (Summary)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/results` | API token | Push run results from CLI/CI |
| GET | `/api/v1/results` | API token | List runs for project |
| GET | `/api/v1/results/[runId]` | API token | Get single run with diffs |
| GET | `/api/v1/goldens` | API token | List golden baselines |
| PUT | `/api/v1/goldens` | API token | Upload/sync golden |
| GET | `/api/v1/goldens/[testName]` | API token | Download golden JSON |
| DELETE | `/api/v1/goldens/[testName]` | API token | Delete golden |
| POST | `/api/v1/projects` | Session | Create project |
| GET | `/api/v1/projects` | Session | List user's projects |
| POST | `/api/v1/tokens` | Session | Create API token |
| GET | `/api/v1/tokens` | Session | List tokens (prefix only) |
| DELETE | `/api/v1/tokens/[id]` | Session | Revoke token |
| POST | `/api/v1/webhook/github` | HMAC | Receive GitHub webhook |
| POST | `/api/v1/billing/checkout` | Session | Create Stripe checkout |
| POST | `/api/v1/billing/webhook` | Stripe sig | Handle Stripe events |
| POST | `/api/v1/judge` | API token | Hosted judge eval (v1.1) |

---

## 7. CLI-to-Cloud Integration

### Changes to `evalview/cloud/client.py`

The existing `CloudClient` currently only handles golden storage. Extend it with result pushing:

```python
# evalview/cloud/client.py — additions

CLOUD_API_URL = "https://evalview.com/api/v1"

class CloudClient:
    """Extended client for EvalView Cloud."""

    def __init__(self, access_token: str = "", api_token: str = "") -> None:
        # Support both OAuth (dashboard) and API token (CLI/CI) auth
        self._api_token = api_token
        self._access_token = access_token

    def _api_headers(self) -> Dict[str, str]:
        token = self._api_token or self._access_token
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def push_result(self, result: Dict[str, Any]) -> Optional[str]:
        """Push a GateResult to the cloud. Returns run URL or None on failure.

        Fire-and-forget with 3 retries, exponential backoff.
        Never raises — failures are logged and swallowed.
        """
        url = f"{CLOUD_API_URL}/results"
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        url, json=result, headers=self._api_headers()
                    )
                    if resp.status_code == 201:
                        return resp.json().get("url")
                    if resp.status_code in (401, 402, 403):
                        return None  # Don't retry auth/billing errors
            except Exception:
                pass
            # Exponential backoff: 1s, 2s, 4s
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        return None

    async def sync_goldens_up(
        self, project_goldens: List[Dict[str, Any]]
    ) -> int:
        """Upload local goldens to cloud. Returns count uploaded."""
        uploaded = 0
        for golden in project_goldens:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.put(
                        f"{CLOUD_API_URL}/goldens",
                        json=golden,
                        headers=self._api_headers(),
                    )
                    if resp.status_code == 200:
                        uploaded += 1
            except Exception:
                continue
        return uploaded
```

### Two-Command Onboarding (Compressed from Five)

```python
# evalview/commands/cloud_cmd.py — new "cloud" command

@main.command("cloud")
@click.option("--token", help="API token (or set EVALVIEW_API_TOKEN)")
def cloud_init(token: str) -> None:
    """Connect this project to EvalView Cloud.

    First time: creates a project, generates an API token, syncs goldens.
    Subsequent: verifies connection, shows status.
    """
    # 1. Resolve token
    api_token = token or os.environ.get("EVALVIEW_API_TOKEN")
    if not api_token:
        auth = CloudAuth()
        if not auth.is_logged_in():
            console.print("[yellow]Run 'evalview login' first.[/yellow]")
            return
        api_token = auth.get_access_token()

    # 2. Create or find project
    client = CloudClient(api_token=api_token)
    project = asyncio.run(client.get_or_create_project(
        name=Path.cwd().name,
        slug=Path.cwd().name.lower().replace(" ", "-"),
    ))

    # 3. Save project ID to .evalview/config.yaml
    save_cloud_config(project["id"], project["api_token"])

    # 4. Sync existing goldens
    golden_store = GoldenStore()
    goldens = golden_store.list_all()
    if goldens:
        count = asyncio.run(client.sync_goldens_up(goldens))
        console.print(f"[green]Synced {count} golden baselines to cloud[/green]")

    console.print(Panel(
        f"Project: {project['name']}\n"
        f"Dashboard: {project['url']}\n"
        f"Token saved to .evalview/config.yaml",
        title="Connected to EvalView Cloud",
        border_style="green",
    ))
```

### Auto-Push After Check/Snapshot

The `--cloud` flag (or `EVALVIEW_CLOUD=true` env var) triggers async push:

```python
# evalview/commands/shared.py — add at end of check execution

async def _maybe_push_to_cloud(gate_result: GateResult) -> None:
    """Push results to cloud if configured. Fire-and-forget."""
    api_token = os.environ.get("EVALVIEW_API_TOKEN") or _load_cloud_token()
    if not api_token:
        return

    client = CloudClient(api_token=api_token)
    url = await client.push_result({
        "run_id": gate_result.raw_json.get("run_id", uuid4().hex[:8]),
        "status": gate_result.status.value,
        "source": "ci" if os.environ.get("CI") else "cli",
        "git_sha": _get_git_sha(),
        "git_branch": _get_git_branch(),
        "git_pr": _get_pr_number(),
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
        "full_result": gate_result.raw_json,
    })
    if url:
        console.print(f"[dim]☁ Results: {url}[/dim]")
```

---

## 8. Dashboard Pages & Components

### Page Map

| Page | URL | What It Shows |
|------|-----|---------------|
| **Dashboard Home** | `/` (authed) | Org list, recent runs across all projects |
| **Project Overview** | `/[org]/[project]` | Test list with status badges, latest run summary, quick stats |
| **Test Detail** | `/[org]/[project]/tests/[name]` | Golden baseline viewer, diff history for this test |
| **Run History** | `/[org]/[project]/runs` | Paginated table of runs with status, source, git info |
| **Run Detail** | `/[org]/[project]/runs/[id]` | Full diff viewer: side-by-side tool calls, output diff, scores |
| **Trends** | `/[org]/[project]/trends` | Recharts: pass rate over time, cost trend, latency trend, score distribution |
| **Settings** | `/[org]/settings` | Org name, plan, danger zone (delete) |
| **Members** | `/[org]/members` | Invite, remove, change roles |
| **Billing** | `/[org]/billing` | Current plan, usage meter, upgrade/manage |
| **API Tokens** | `/[org]/[project]/tokens` | Create, list, revoke project tokens |

### Key Components

**Diff Viewer (`diff-viewer.tsx`)**
The core value component. Renders side-by-side diffs of:
- Tool call sequences (added/removed/changed tools, color-coded)
- Output text (line-by-line diff using `diff` npm package)
- Parameter changes (JSON diff with syntax highlighting)
- Score deltas (green/red arrows)

Port the diff logic from `evalview/core/diff.py` and the HTML report from `evalview/visualization/generators.py` into React components.

**Trend Charts (`trend-chart.tsx`)**
Recharts line/area charts:
- Pass rate over time (percentage, area fill)
- Cost per run (line with budget threshold marker)
- Latency per run (line with SLA threshold marker)
- Regressions per week (bar chart)

Data source: aggregate query on `runs` table, grouped by day/week.

**Test Status Badge (`test-status-badge.tsx`)**
Consistent status display matching CLI output:
- `PASSED` → green badge
- `REGRESSION` → red badge
- `TOOLS_CHANGED` → amber badge
- `OUTPUT_CHANGED` → amber badge (lighter)

**Real-Time Updates (`use-realtime.ts`)**
Subscribe to Supabase Realtime on the `runs` table for the current project. When a new run arrives (from CI or another team member), update the dashboard without refresh.

```typescript
// src/hooks/use-realtime.ts
import { useEffect } from "react";
import { createBrowserClient } from "@/lib/supabase/client";

export function useRunsRealtime(projectId: string, onNewRun: (run: any) => void) {
  useEffect(() => {
    const supabase = createBrowserClient();
    const channel = supabase
      .channel(`runs:${projectId}`)
      .on(
        "postgres_changes",
        {
          event: "INSERT",
          schema: "public",
          table: "runs",
          filter: `project_id=eq.${projectId}`,
        },
        (payload) => onNewRun(payload.new)
      )
      .subscribe();

    return () => { supabase.removeChannel(channel); };
  }, [projectId, onNewRun]);
}
```

---

## 9. CI/CD Webhook System

### GitHub Webhook Flow

```
GitHub PR event
    │
    ▼
POST /api/v1/webhook/github
    │
    ├─ Verify HMAC-SHA256 signature
    ├─ Extract: repo, PR number, SHA, action
    ├─ Find matching project by repo URL
    │
    ├─ On "check_run.completed" or "workflow_run.completed":
    │   └─ Find latest run for this SHA
    │       └─ Post/update PR comment via GitHub API
    │
    └─ On "pull_request.opened" or "pull_request.synchronize":
        └─ (Optional) Trigger baseline comparison notification
```

### Webhook Signature Verification

```typescript
// src/lib/webhook-verify.ts
import { createHmac, timingSafeEqual } from "crypto";

export function verifyGitHubWebhook(
  payload: string,
  signature: string | null,
  secret: string
): boolean {
  if (!signature) return false;

  const expected = `sha256=${createHmac("sha256", secret)
    .update(payload)
    .digest("hex")}`;

  try {
    return timingSafeEqual(
      Buffer.from(signature),
      Buffer.from(expected)
    );
  } catch {
    return false;
  }
}
```

### PR Comment Posting (Cloud-Side)

Instead of relying on `gh` CLI in CI, the cloud posts comments via GitHub API using a GitHub App installation token:

```typescript
// src/app/api/v1/webhook/github/route.ts (simplified)
async function postPRComment(
  installationId: number,
  owner: string,
  repo: string,
  prNumber: number,
  run: Run
) {
  const token = await getInstallationToken(installationId);
  const octokit = new Octokit({ auth: token });

  // Reuse comment.py's markdown format
  const body = generateCheckComment(run);

  // Find existing EvalView comment
  const { data: comments } = await octokit.issues.listComments({
    owner, repo, issue_number: prNumber,
  });

  const existing = comments.find((c) =>
    c.body?.includes("Generated by [EvalView]")
  );

  if (existing) {
    await octokit.issues.updateComment({
      owner, repo, comment_id: existing.id, body,
    });
  } else {
    await octokit.issues.createComment({
      owner, repo, issue_number: prNumber, body,
    });
  }
}
```

### Updated action.yml

Add cloud token support to the existing GitHub Action:

```yaml
# action.yml — additions
inputs:
  cloud-token:
    description: 'EvalView Cloud API token for result reporting'
    required: false
  cloud-url:
    description: 'EvalView Cloud API URL (default: https://evalview.com/api/v1)'
    required: false
    default: 'https://evalview.com/api/v1'

# In steps, after test execution:
- name: Push results to cloud
  if: ${{ inputs.cloud-token != '' }}
  env:
    EVALVIEW_API_TOKEN: ${{ inputs.cloud-token }}
  run: |
    evalview cloud push --results "${{ steps.run.outputs.results-file }}"
```

---

## 10. Billing (Stripe)

### Plan Configuration

```typescript
// src/lib/stripe/plans.ts
export const PLANS = {
  free: {
    name: "Starter",
    priceMonthly: 0,
    priceAnnual: 0,
    stripePriceIdMonthly: null,
    stripePriceIdAnnual: null,
    limits: {
      testRuns: 100,
      projects: 2,
      members: 1,
      storageMb: 100,
      judgeEvals: 50,
      retentionDays: 7,
      prComments: false,
      slackAlerts: false,
    },
  },
  team: {
    name: "Team",
    priceMonthly: 49_00,  // cents
    priceAnnual: 468_00,  // $39/mo billed annually
    stripePriceIdMonthly: "price_xxx",
    stripePriceIdAnnual: "price_yyy",
    limits: {
      testRuns: 10_000,
      projects: -1,  // unlimited
      members: 10,
      storageMb: 5_120,
      judgeEvals: 2_000,
      retentionDays: 90,
      prComments: true,
      slackAlerts: true,
    },
  },
  enterprise: {
    name: "Enterprise",
    priceMonthly: null,  // custom
    priceAnnual: null,
    stripePriceIdMonthly: null,
    stripePriceIdAnnual: null,
    limits: {
      testRuns: -1,
      projects: -1,
      members: -1,
      storageMb: -1,
      judgeEvals: -1,
      retentionDays: 365,
      prComments: true,
      slackAlerts: true,
    },
  },
} as const;
```

### Stripe Webhook Handler

```typescript
// src/app/api/v1/billing/webhook/route.ts
import Stripe from "stripe";

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!);

export async function POST(req: NextRequest) {
  const body = await req.text();
  const sig = req.headers.get("stripe-signature")!;

  let event: Stripe.Event;
  try {
    event = stripe.webhooks.constructEvent(
      body, sig, process.env.STRIPE_WEBHOOK_SECRET!
    );
  } catch {
    return NextResponse.json({ error: "Invalid signature" }, { status: 400 });
  }

  switch (event.type) {
    case "checkout.session.completed": {
      const session = event.data.object as Stripe.Checkout.Session;
      const orgId = session.metadata?.org_id;
      if (orgId) {
        await supabaseAdmin
          .from("organizations")
          .update({
            plan: "team",
            stripe_customer_id: session.customer as string,
            stripe_subscription_id: session.subscription as string,
          })
          .eq("id", orgId);
      }
      break;
    }

    case "customer.subscription.deleted": {
      const sub = event.data.object as Stripe.Subscription;
      await supabaseAdmin
        .from("organizations")
        .update({ plan: "free", stripe_subscription_id: null })
        .eq("stripe_subscription_id", sub.id);
      break;
    }

    case "customer.subscription.updated": {
      const sub = event.data.object as Stripe.Subscription;
      const plan = sub.items.data[0]?.price.id === PLANS.team.stripePriceIdAnnual
        ? "team" : "team";  // Extend when adding more tiers
      await supabaseAdmin
        .from("organizations")
        .update({ plan })
        .eq("stripe_subscription_id", sub.id);
      break;
    }

    case "invoice.payment_failed": {
      // Send dunning email, don't downgrade immediately
      // Grace period: 7 days before downgrade
      break;
    }
  }

  return NextResponse.json({ received: true });
}
```

### Usage Enforcement

```typescript
// src/lib/stripe/usage.ts
export async function checkUsageLimits(
  orgId: string,
  plan: string,
  metric: "test_runs" | "judge_evals",
  increment: number
): Promise<boolean> {
  const limits = PLANS[plan as keyof typeof PLANS]?.limits;
  if (!limits) return false;

  const maxValue = metric === "test_runs" ? limits.testRuns : limits.judgeEvals;
  if (maxValue === -1) return true; // unlimited

  const period = new Date().toISOString().slice(0, 7); // "2026-03"
  const { data } = await supabaseAdmin
    .from("usage")
    .select(metric)
    .eq("org_id", orgId)
    .eq("period", period)
    .single();

  const current = data?.[metric] ?? 0;
  return (current + increment) <= maxValue;
}
```

### Downgrade Handling

When a team downgrades from Team → Free:
1. Plan changes immediately in `organizations` table.
2. Existing data beyond free limits is **read-only for 30 days** (grace period).
3. After 30 days, runs older than 7 days are archived (not deleted — moved to cold storage).
4. Projects beyond the limit of 2 are set to `archived` status (visible but can't push new results).
5. Members beyond 1 lose access but aren't removed (can be restored on re-upgrade).

---

## 11. Hosted Judge (v1.1)

Supabase Edge Function that proxies LLM judge evaluation calls:

```typescript
// supabase/functions/hosted-judge/index.ts
import { serve } from "https://deno.land/std/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js";

serve(async (req) => {
  // Verify API token
  const ctx = await verifyApiToken(req.headers.get("authorization"));
  if (!ctx) return new Response("Unauthorized", { status: 401 });

  // Check judge eval quota
  const withinLimits = await checkUsageLimits(ctx.orgId, ctx.plan, "judge_evals", 1);
  if (!withinLimits) {
    return new Response(JSON.stringify({
      error: "Judge evaluation limit reached for this billing period",
    }), { status: 402 });
  }

  const { prompt, model, provider } = await req.json();

  // Route to provider
  const apiKey = provider === "anthropic"
    ? Deno.env.get("ANTHROPIC_API_KEY")
    : Deno.env.get("OPENAI_API_KEY");

  // Call LLM and return judge result
  const result = await callLLM(provider, model, prompt, apiKey);

  // Increment usage
  await incrementUsage(ctx.orgId, 0, 1, 0);

  return new Response(JSON.stringify(result), {
    headers: { "Content-Type": "application/json" },
  });
});
```

CLI integration — `evalview/core/llm_provider.py` gets a new provider:

```python
# In llm_provider.py — add "cloud" provider
class CloudJudgeProvider:
    """Proxy judge calls through EvalView Cloud (uses cloud API keys)."""

    async def evaluate(self, prompt: str) -> str:
        api_token = os.environ.get("EVALVIEW_API_TOKEN") or _load_cloud_token()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{CLOUD_API_URL}/judge",
                json={"prompt": prompt, "model": "gpt-4o", "provider": "openai"},
                headers={"Authorization": f"Bearer {api_token}"},
            )
            resp.raise_for_status()
            return resp.json()["result"]
```

---

## 12. CLI Changes Required

Summary of all changes needed in the existing Python CLI:

| File | Change | Phase |
|------|--------|-------|
| `evalview/cloud/client.py` | Add `push_result()`, `sync_goldens_up()`, `get_or_create_project()` | Alpha |
| `evalview/cloud/auth.py` | Add `api_token` field support alongside OAuth tokens | Alpha |
| `evalview/commands/cloud_cmd.py` | Add `cloud` command (init + sync in one step) | Alpha |
| `evalview/commands/shared.py` | Add `_maybe_push_to_cloud()` after check/snapshot execution | Alpha |
| `evalview/commands/check_cmd.py` | Add `--cloud` flag, call `_maybe_push_to_cloud()` | Alpha |
| `evalview/commands/snapshot_cmd.py` | Add golden sync-up call when cloud is configured | Alpha |
| `evalview/core/config.py` | Add `CloudConfig` model (api_token, project_id, auto_push) | Alpha |
| `action.yml` | Add `cloud-token` input and push step | Beta |
| `evalview/core/llm_provider.py` | Add `CloudJudgeProvider` | v1.1 |
| `evalview/api.py` | Add `cloud_push` param to `gate()` | Beta |

### Environment Variables (New)

| Variable | Purpose | Required |
|----------|---------|----------|
| `EVALVIEW_API_TOKEN` | Project-scoped API token for cloud push | For cloud features |
| `EVALVIEW_CLOUD` | Set to `true` to auto-push results | Optional (alternative to --cloud flag) |
| `EVALVIEW_CLOUD_URL` | Cloud API base URL (default: `https://evalview.com/api/v1`) | Optional (for self-hosted) |

---

## 13. Deployment & Infrastructure

### Vercel (Next.js)

```
cloud/
├── vercel.json
│   {
│     "framework": "nextjs",
│     "regions": ["iad1"],          // US East (close to most CI runners)
│     "env": {
│       "SUPABASE_URL": "@supabase-url",
│       "SUPABASE_SERVICE_ROLE_KEY": "@supabase-service-key",
│       "STRIPE_SECRET_KEY": "@stripe-secret",
│       "STRIPE_WEBHOOK_SECRET": "@stripe-webhook-secret",
│       "GITHUB_APP_PRIVATE_KEY": "@github-app-key"
│     }
│   }
```

### Supabase Project

- **Region:** US East (us-east-1) — co-located with Vercel
- **Plan:** Supabase Pro ($25/mo) — needed for Edge Functions and pg_cron
- **Storage:** `goldens` bucket (already exists), add `archives` bucket for downgraded accounts
- **Realtime:** Enabled on `runs` table for dashboard live updates
- **pg_cron:** Schedule `cleanup_rate_limits()` daily, `archive_expired_runs()` weekly

### Domain Structure

| Domain | Points To |
|--------|-----------|
| `evalview.com` | Vercel (marketing + app) |
| `app.evalview.com` | Vercel (dashboard, if you want to separate) |
| `api.evalview.com` | Vercel `/api/v1/*` routes (optional, can use same domain) |

### Monitoring

- **Vercel Analytics** — Page performance, API latency
- **Supabase Dashboard** — DB size, active connections, auth stats
- **Stripe Dashboard** — MRR, churn, failed payments
- **Sentry** — Error tracking (both Next.js and Edge Functions)
- **Simple uptime check** — ping `/api/v1/health` every minute (Uptime Robot, free)

---

## 14. Phased Delivery Plan

Reordered from original blueprint based on revenue-priority analysis.

### Phase 1: Alpha (Weeks 1–5)

**Goal:** CLI users can push results to cloud and see them in a dashboard.

| Week | Deliverable |
|------|-------------|
| 1 | Supabase migrations (schema, RLS, functions). Next.js project scaffold with auth (GitHub OAuth). |
| 2 | `POST /api/v1/results` endpoint. CLI changes: `push_result()`, `--cloud` flag, `evalview cloud` command. |
| 3 | Dashboard: project page (test list + status badges), run history table, run detail page. |
| 4 | Golden sync: `PUT/GET /api/v1/goldens`, bidirectional sync in CLI. Dashboard: golden viewer. |
| 5 | API token management (create/list/revoke). Dashboard: diff viewer component. Polish + dogfood. |

**Alpha exit criteria:**
- A user can `evalview login` → `evalview cloud` → `evalview check --cloud` and see results on the dashboard.
- Golden baselines sync between local and cloud.
- API tokens work for headless (CI) access.

### Phase 2: Beta (Weeks 6–9)

**Goal:** Teams can use EvalView Cloud in CI with PR comments.

| Week | Deliverable |
|------|-------------|
| 6 | GitHub App setup. Webhook receiver (`POST /api/v1/webhook/github`). PR comment posting from cloud. |
| 7 | `action.yml` update with `cloud-token` input. CI end-to-end flow tested. |
| 8 | Team support: org members, invites, role management. Dashboard: members page, org switcher. |
| 9 | Trends page (Recharts). Realtime dashboard updates. Usage tracking tables and `increment_usage()`. |

**Beta exit criteria:**
- A team of 3 can share a project, push results from CI, and see PR comments automatically.
- Usage is tracked per org per month.

### Phase 3: GA (Weeks 10–13)

**Goal:** Billing is live. Free tier enforced. Public launch.

| Week | Deliverable |
|------|-------------|
| 10 | Stripe integration: checkout, webhook, plan management. Dashboard: billing page, upgrade prompts. |
| 11 | Usage enforcement: rate limiting, plan limit checks on API routes, upgrade-required errors. |
| 12 | Landing page (evalview.com/cloud). Docs site. Onboarding flow (first-time user walkthrough). |
| 13 | Load testing, security audit (OWASP top 10), penetration test on API routes. Launch. |

**GA exit criteria:**
- Users can sign up, use free tier, upgrade to Team, manage billing.
- Usage limits are enforced. Rate limiting works.
- Landing page converts visitors to signups.

### Phase 4: v1.1 (Weeks 14–21)

| Week | Deliverable |
|------|-------------|
| 14–15 | Hosted judge Edge Function. CLI `CloudJudgeProvider`. Usage metering for judge evals. |
| 16–17 | Slack/Discord alert integration (cloud-side, not just CLI monitor). |
| 18–19 | GitLab CI support. Generic webhook support (non-GitHub). |
| 20–21 | Enterprise: SSO (SAML via Supabase Auth), audit log, custom retention. |

---

## 15. Open Questions & Decisions

These need answers before or during implementation:

| # | Question | Options | Recommendation |
|---|----------|---------|----------------|
| 1 | **Monorepo or separate repo for cloud?** | (a) `cloud/` in eval-view repo, (b) separate `evalview-cloud` repo | **(a) Monorepo.** CLI changes and cloud changes will be tightly coupled during Alpha/Beta. Split later if needed. |
| 2 | **Golden storage: Supabase Storage or Postgres JSONB?** | (a) Storage bucket (current), (b) JSONB column in `goldens` table | **(a) Storage.** Goldens can be large (multi-turn traces). Storage is cheaper and doesn't bloat the DB. Keep metadata in Postgres, files in Storage. |
| 3 | **GitHub integration: OAuth App or GitHub App?** | (a) OAuth App (simpler, user-scoped), (b) GitHub App (installation-scoped, can post as bot) | **(b) GitHub App.** Posting PR comments requires repo access. A GitHub App can be installed per-repo with minimal permissions and posts as "EvalView [bot]". |
| 4 | **Self-hosted option?** | (a) Cloud-only, (b) Docker Compose for self-hosted | **(a) Cloud-only for now.** Self-hosted fragments your support surface and reduces conversion. Revisit for enterprise tier. |
| 5 | **Result retention on downgrade** | (a) Hard delete after grace period, (b) Archive to cold storage, (c) Keep forever but read-only | **(b) Archive.** Move to a separate `archives` Storage bucket. Restoreable on re-upgrade. Avoids data loss complaints while controlling costs. |
| 6 | **Pricing: $49 for 10 seats or $49 for 3 seats?** | See previous analysis | **$49 for 3 seats, $15/additional.** Gets you to ~$100/mo for a real team. Test with early users and adjust. |
| 7 | **Free tier: 500 or 100 runs/month?** | See previous analysis | **100 runs/month.** Enough for evaluation, creates natural upgrade friction for active users. |

---

## Appendix: Data Size Estimates

For capacity planning and Supabase tier selection:

| Entity | Avg Size | 100 free users | 20 team accounts |
|--------|----------|----------------|-------------------|
| Run (result_json) | ~5 KB | 50K runs/mo → 250 MB | 200K runs/mo → 1 GB |
| Test diff (diff_json) | ~2 KB | 150K/mo → 300 MB | 600K/mo → 1.2 GB |
| Golden baseline | ~10 KB | 500 files → 5 MB | 2K files → 20 MB |
| Total DB (monthly) | — | ~550 MB | ~2.2 GB |

Supabase Pro (8 GB included) handles this comfortably for the first year. Add automated archival (move runs older than retention to Storage) to keep the DB lean.
