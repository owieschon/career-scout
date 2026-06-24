-- Alice — Phase D — Supabase schema (v0001 init)
-- Multi-tenant from day one. Every row is scoped by user_id; RLS enforces it.
-- The daemon authenticates with the service_role key (bypasses RLS) and
-- explicitly filters by user_id; RLS is defense-in-depth for any client that
-- ever hits the DB with the anon key.
--
-- Idempotent: safe to re-run. Uses IF NOT EXISTS / CREATE OR REPLACE / ON CONFLICT
-- where applicable.

-- 1. Users registry (one row per Alice tenant). FK target for everything else.
--    We do NOT couple to Supabase Auth here; the daemon owns the user_id and
--    Phase 2 (chat-first intake) decides whether to back this with auth.users.
create table if not exists app_users (
    user_id     text primary key,
    handle      text unique not null,
    created_at  timestamptz not null default now(),
    notes       text
);

-- 2. Roles — the surfaced/pipeline record. One logical row per (user, job_key).
--    Mirrors the Sheet columns; preserves the API shape consumers depend on.
create table if not exists roles (
    id                  bigserial primary key,
    user_id             text not null references app_users(user_id) on delete cascade,
    job_key             text not null,
    surfaced_date       date,
    company             text,
    role                text,
    comp                text,
    source              text,
    score               text,                  -- text to mirror Sheet's free-form col F (sometimes blank, sometimes int)
    status              text not null default 'new',
    notes               text default '',
    url                 text,
    rationale           text default '',
    status_changed_date date,
    intent              text default '',
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint roles_user_jobkey_uk unique (user_id, job_key)
);

create index if not exists roles_user_status_idx
    on roles (user_id, status);
create index if not exists roles_user_company_idx
    on roles (user_id, lower(company));
create index if not exists roles_user_surfaced_idx
    on roles (user_id, surfaced_date desc);

-- Updated-at trigger (so application code never has to remember to bump it).
create or replace function _touch_updated_at() returns trigger
    language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists roles_touch_updated_at on roles;
create trigger roles_touch_updated_at
    before update on roles
    for each row execute function _touch_updated_at();

-- 3. Status history — append-only audit of every status change. Mirrors the
--    Sheet write-log JSONL but as queryable rows. Drives auto_drop's
--    "was this write authorized" check and Phoenix outcome traces.
create table if not exists status_history (
    id          bigserial primary key,
    role_id     bigint not null references roles(id) on delete cascade,
    user_id     text not null references app_users(user_id) on delete cascade,
    status      text not null,
    changed_at  timestamptz not null default now(),
    authorized  boolean not null default false,
    source      text default 'unspecified'
);

create index if not exists status_history_role_idx
    on status_history (role_id, changed_at desc);
create index if not exists status_history_user_idx
    on status_history (user_id, changed_at desc);

-- 4. Fit verdicts — judge output, one row per (role, judge_model, run).
--    Lets us track judge drift and per-model agreement without overwriting
--    the role's denormalized `score`.
create table if not exists fit_verdicts (
    id           bigserial primary key,
    role_id      bigint not null references roles(id) on delete cascade,
    user_id      text not null references app_users(user_id) on delete cascade,
    judge_model  text not null,
    verdict      text not null,
    consistent   boolean,
    score        integer,
    reason       text,
    created_at   timestamptz not null default now()
);

create index if not exists fit_verdicts_role_idx
    on fit_verdicts (role_id, created_at desc);

-- 5. Sources registry — per-user list of enabled sourcing boards (Phase E will
--    populate this dynamically; Phase D just provisions the table).
create table if not exists sources (
    id          bigserial primary key,
    user_id     text not null references app_users(user_id) on delete cascade,
    kind        text not null,                 -- 'greenhouse' | 'ashby' | 'lever' | 'yc' | 'vc' | ...
    slug        text not null,                 -- board id on that platform
    enabled     boolean not null default true,
    added_at    timestamptz not null default now(),
    last_seen_at timestamptz,
    constraint sources_user_kind_slug_uk unique (user_id, kind, slug)
);

create index if not exists sources_user_enabled_idx
    on sources (user_id, enabled);

-- 6. OOS review queue — preserves the second tab "OOS Review (Alice)" from the
--    Sheet. Phase D migrates rows; oos_eval.py is off-limits this phase so the
--    Sheet remains canonical until CC re-wires it.
create table if not exists oos_review (
    id             bigserial primary key,
    user_id        text not null references app_users(user_id) on delete cascade,
    found_date     date,
    company        text,
    role           text,
    verdict        text,
    consistent     text,                       -- 'yes' | 'WOBBLED' (mirrors Sheet)
    score          integer,
    url            text,
    judge_reason   text,
    operator_decision  text default '',
    created_at     timestamptz not null default now(),
    constraint oos_review_user_url_uk unique (user_id, url)
);

create index if not exists oos_review_user_decision_idx
    on oos_review (user_id, operator_decision);

-- 7. RLS — every table is denied-by-default and only readable/writable when
--    the JWT's `sub` matches the row's user_id. The service_role key bypasses
--    this (it's what the daemon uses); the anon/auth key cannot leak across
--    tenants.
alter table app_users      enable row level security;
alter table roles          enable row level security;
alter table status_history enable row level security;
alter table fit_verdicts   enable row level security;
alter table sources        enable row level security;
alter table oos_review     enable row level security;

-- Helper: extract the calling user's id from the JWT. Falls through to NULL
-- when there is no JWT (the service_role path skips RLS entirely).
create or replace function _alice_uid() returns text
    language sql stable as $$
    select coalesce(
        nullif(current_setting('request.jwt.claim.sub', true), ''),
        nullif(current_setting('request.jwt.claims', true)::jsonb->>'sub', '')
    );
$$;

-- Drop+recreate policies idempotently.
do $$
declare t text;
begin
    for t in
        select unnest(array['app_users','roles','status_history','fit_verdicts','sources','oos_review'])
    loop
        execute format('drop policy if exists %I_select on %I', t || '_tenant_select', t);
        execute format('drop policy if exists %I_modify on %I', t || '_tenant_modify', t);
    end loop;
end$$;

create policy app_users_tenant_select on app_users
    for select using (user_id = _alice_uid());
create policy app_users_tenant_modify on app_users
    for all using (user_id = _alice_uid()) with check (user_id = _alice_uid());

create policy roles_tenant_select on roles
    for select using (user_id = _alice_uid());
create policy roles_tenant_modify on roles
    for all using (user_id = _alice_uid()) with check (user_id = _alice_uid());

create policy status_history_tenant_select on status_history
    for select using (user_id = _alice_uid());
create policy status_history_tenant_modify on status_history
    for all using (user_id = _alice_uid()) with check (user_id = _alice_uid());

create policy fit_verdicts_tenant_select on fit_verdicts
    for select using (user_id = _alice_uid());
create policy fit_verdicts_tenant_modify on fit_verdicts
    for all using (user_id = _alice_uid()) with check (user_id = _alice_uid());

create policy sources_tenant_select on sources
    for select using (user_id = _alice_uid());
create policy sources_tenant_modify on sources
    for all using (user_id = _alice_uid()) with check (user_id = _alice_uid());

create policy oos_review_tenant_select on oos_review
    for select using (user_id = _alice_uid());
create policy oos_review_tenant_modify on oos_review
    for all using (user_id = _alice_uid()) with check (user_id = _alice_uid());

-- 8. Schema version marker. Lets the migration script no-op safely when the
--    schema is already at the right version, and lets future migrations chain.
create table if not exists schema_versions (
    version    integer primary key,
    applied_at timestamptz not null default now(),
    note       text
);

insert into schema_versions (version, note)
    values (1, 'init: roles, status_history, fit_verdicts, sources, oos_review, RLS')
    on conflict (version) do nothing;
