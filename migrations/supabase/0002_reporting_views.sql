-- Alice — reporting views (v0002)
--
-- Aggregation belongs in the database, not in Python row loops. These views
-- express the pipeline's reporting needs over the v0001 tables. The same query
-- logic is unit-tested against SQLite in tests/test_reporting.py (scripts/
-- reporting.py); these are the Postgres-native equivalents the Supabase backend
-- serves through PostgREST.
--
-- Every view is declared WITH (security_invoker = true) so the row-level
-- security policies from v0001 still apply: a tenant querying a view sees an
-- aggregate over ONLY its own rows, never across tenants. (Without this, a view
-- would run as its owner and silently bypass RLS.)
--
-- Idempotent: CREATE OR REPLACE VIEW throughout.

-- Pipeline distribution — role counts by status, ordered along the real funnel
-- (new -> ... -> offered, 'not a fit' parked last) rather than alphabetically.
create or replace view v_pipeline_funnel with (security_invoker = true) as
with funnel_order(status, ord) as (
    values ('new', 0), ('good fit', 1), ('materials pending', 2),
           ('submitted', 3), ('interviewed', 4), ('offered', 5),
           ('not a fit', 9)
),
counts as (
    select status, count(*) as n
    from roles
    group by status
)
select c.status, c.n
from counts c
left join funnel_order f on f.status = c.status
order by coalesce(f.ord, 99), c.status;

-- Company suppression — per company, how many roles were marked 'not a fit' vs
-- 'good fit'. The sourcing pipeline suppresses companies it has repeatedly
-- rejected; this is the signal, computed in one pass with conditional
-- aggregation instead of two Python counters over every row.
create or replace view v_company_suppression with (security_invoker = true) as
select
    company,
    count(*) filter (where status = 'not a fit') as not_fit,
    count(*) filter (where status = 'good fit')  as good_fit,
    count(*)                                     as total
from roles
where company is not null and company <> ''
group by company
having count(*) filter (where status in ('not a fit', 'good fit')) > 0
order by not_fit desc, good_fit desc, company;

-- Judge-drift monitor — per judge model, the verdict mix and the rate at which
-- the consistency (second-pass) check agreed with the first verdict. A falling
-- agreement rate flags a drifting model; this is why fit_verdicts retains every
-- run instead of overwriting the role's denormalized score.
create or replace view v_judge_verdict_distribution with (security_invoker = true) as
select
    judge_model,
    count(*)                                   as verdicts,
    count(*) filter (where verdict = 'FIT')      as fit,
    count(*) filter (where verdict = 'NOT-FIT')  as not_fit,
    count(*) filter (where verdict = 'REACH')    as reach,
    round(100.0 * count(*) filter (where consistent) / nullif(count(*) filter (where consistent is not null), 0), 1) as agreement_pct
from fit_verdicts
group by judge_model
order by verdicts desc;

-- Status-transition frequency — which (from -> to) status changes occur and how
-- often. LEAD walks each role's history chronologically so consecutive pairs
-- fall out without a self-join, surfacing where the pipeline flows and stalls.
create or replace view v_status_transitions with (security_invoker = true) as
with transitions as (
    select
        status as from_status,
        lead(status) over (partition by role_id order by changed_at, id) as to_status
    from status_history
)
select from_status, to_status, count(*) as n
from transitions
where to_status is not null
group by from_status, to_status
order by n desc, from_status, to_status;

-- Time-in-stage — average days a role spends at each status before its next
-- change, via a window function plus interval arithmetic. Postgres-native
-- (the SQLite test covers transition counts; duration math lives here where the
-- type system makes it exact). Surfaces stages where roles stall.
create or replace view v_time_in_stage with (security_invoker = true) as
with ordered as (
    select
        role_id, status, changed_at,
        lead(changed_at) over (partition by role_id order by changed_at, id) as next_changed_at
    from status_history
)
select
    status,
    count(*) filter (where next_changed_at is not null) as transitions,
    round(
        avg(extract(epoch from (next_changed_at - changed_at)) / 86400.0)
            filter (where next_changed_at is not null),
        1
    ) as avg_days_in_stage
from ordered
group by status
order by avg_days_in_stage desc nulls last;

insert into schema_versions (version, note)
    values (2, 'reporting views: funnel, company suppression, judge drift, transitions, time-in-stage')
    on conflict (version) do nothing;
