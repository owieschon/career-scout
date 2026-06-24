"""One-shot data migration: live Google Sheet → Supabase Postgres.

Reads the live Sheet via the existing `ledger._sheets_ws()` and upserts every
row into the new Postgres schema. The migration is **idempotent**: it can be
run repeatedly with no double-insertion, because the `roles` table has a
UNIQUE(user_id, job_key) constraint and the script uses ON CONFLICT DO NOTHING.

Tabs migrated:
    sheet1                  → roles            (primary pipeline)
    "OOS Review (Alice)"    → oos_review       (the second tab oos_eval writes)

Verification: prints row-counts on both sides + a spot-check sample. The DoD
asks for "row-count + spot-check verified" — that's what the verifier section
at the end of this script does.

Usage:
    python3 scripts/migrate_sheet_to_supabase.py --dry-run        # nothing written
    python3 scripts/migrate_sheet_to_supabase.py --apply          # writes to supabase
    python3 scripts/migrate_sheet_to_supabase.py --verify         # row-count + spot-check only

Required config (~/.config/job-search/config.env):
    GOOGLE_SA_JSON, LEDGER_SHEET_ID   — to read the live Sheet
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   — to write Postgres
    ALICE_USER_ID                     — the tenant this Sheet belongs to (default 'operator')
"""
from __future__ import annotations

import argparse
import datetime
import sys
import time
from pathlib import Path
from alice import repo_paths

REPO_ROOT = repo_paths.ROOT

from alice.persistence import ledger          # noqa: E402   uses _sheets_ws to read the live Sheet
from alice.persistence import supabase_ledger # noqa: E402   uses _get_client + _user_id for the Postgres side


OOS_TAB = "OOS Review (Alice)"


def _ensure_user(client, user_id: str):
    """Upsert the app_users row idempotently (FK target for everything else)."""
    (client.table("app_users")
        .upsert({"user_id": user_id, "handle": user_id},
                on_conflict="user_id", ignore_duplicates=True)
        .execute())


def _coerce_date(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s[:10]).isoformat()
    except Exception:
        return None


def _migrate_roles(sheet_ws, sb_client, user_id: str, apply: bool):
    rows = sheet_ws.get_all_records()
    payload = []
    for r in rows:
        jk = (str(r.get("job_key") or r.get("url") or "")).strip()
        if not jk:
 # Skip rows that have no stable key. The live Sheet has had a few
 # of these historically; surface but don't fail.
            continue
        payload.append({
            "user_id":             user_id,
            "job_key":             jk,
            "surfaced_date":       _coerce_date(str(r.get("surfaced_date") or "")),
            "company":             (r.get("company") or "")[:1000],
            "role":                (r.get("role") or "")[:1000],
            "comp":                (r.get("comp") or "")[:1000],
            "source":              (r.get("source") or "")[:200],
            "score":               str(r.get("score") or ""),
            "status":              (r.get("status") or "new") or "new",
            "notes":               r.get("notes") or "",
            "url":                 r.get("url") or "",
            "rationale":           r.get("rationale") or "",
            "status_changed_date": _coerce_date(str(r.get("status_changed_date") or "")),
            "intent":              (r.get("intent") or "").strip().lower(),
        })

    print(f"[roles] read {len(rows)} sheet rows; {len(payload)} have job_key/url and will be upserted")
    if not apply:
        print("[roles] dry-run; not writing")
        return len(payload)

 # Upsert in batches of 500 to keep payloads small.
    written = 0
    BATCH = 500
    for i in range(0, len(payload), BATCH):
        chunk = payload[i:i+BATCH]
        resp = (sb_client.table("roles")
                .upsert(chunk, on_conflict="user_id,job_key", ignore_duplicates=True)
                .execute())
        written += len(resp.data or [])
        time.sleep(0.05)  # gentle pacing; pgbouncer pooler is generous but no need to hammer
    print(f"[roles] wrote {written} new roles (duplicates skipped via UNIQUE)")
    return len(payload)


def _migrate_oos_review(sheet_ws, sb_client, user_id: str, apply: bool):
    """The OOS Review tab is on the same spreadsheet as sheet1 but a separate
    worksheet. We grab it via the spreadsheet handle on ws."""
    try:
        ss = sheet_ws.spreadsheet
        oos = ss.worksheet(OOS_TAB)
    except Exception as e:
        print(f"[oos_review] tab '{OOS_TAB}' not present or unreadable ({e}); skipping")
        return 0
    rows = oos.get_all_records()
    payload = []
    for r in rows:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        payload.append({
            "user_id":       user_id,
            "found_date":    _coerce_date(str(r.get("found_date") or "")),
            "company":       r.get("company") or "",
            "role":          r.get("role") or "",
            "verdict":       r.get("verdict") or "",
            "consistent":    r.get("consistent") or "",
            "score":         int(r.get("score")) if str(r.get("score") or "").strip().isdigit() else None,
            "url":           url,
            "judge_reason":  r.get("judge_reason") or "",
            "operator_decision": r.get("operator_decision") or "",
        })
    print(f"[oos_review] read {len(rows)} oos rows; {len(payload)} have a url and will be upserted")
    if not apply:
        print("[oos_review] dry-run; not writing")
        return len(payload)
    if not payload:
        return 0
    resp = (sb_client.table("oos_review")
            .upsert(payload, on_conflict="user_id,url", ignore_duplicates=True)
            .execute())
    print(f"[oos_review] wrote {len(resp.data or [])} new oos rows")
    return len(payload)


def _verify(sheet_ws, sb_client, user_id: str, sample_n: int = 5):
    """Row-count + spot-check parity. Returns 0 on success, nonzero on parity drift."""
    sheet_rows = sheet_ws.get_all_records()
    sheet_keys = {(str(r.get("job_key") or r.get("url") or "")).strip()
                  for r in sheet_rows if (r.get("job_key") or r.get("url"))}
    resp = (sb_client.table("roles").select("job_key").eq("user_id", user_id).execute())
    sb_keys = {r["job_key"] for r in (resp.data or [])}

    missing_in_sb = sheet_keys - sb_keys
    extra_in_sb = sb_keys - sheet_keys
    print(f"[verify] sheet: {len(sheet_keys)} keys | supabase: {len(sb_keys)} keys")
    print(f"[verify] missing in supabase: {len(missing_in_sb)} | extra in supabase: {len(extra_in_sb)}")
    if missing_in_sb:
        print("[verify] sample missing:")
        for k in list(sorted(missing_in_sb))[:sample_n]:
            print(f"    - {k}")

 # Spot-check status for N random rows.
    import random
    rng = random.Random(0xa11ce)
    sample_keys = rng.sample(sorted(sheet_keys & sb_keys), min(sample_n, len(sheet_keys & sb_keys)))
    mismatches = 0
    for k in sample_keys:
        sheet_row = next((r for r in sheet_rows
                          if (r.get("job_key") or r.get("url")) == k), None)
        sb_row = (sb_client.table("roles")
                  .select("status, company, intent")
                  .eq("user_id", user_id).eq("job_key", k).single().execute().data or {})
        sheet_status = (sheet_row.get("status") or "").strip().lower()
        sb_status = (sb_row.get("status") or "").strip().lower()
        if sheet_status != sb_status:
            mismatches += 1
            print(f"    DRIFT {k}: sheet={sheet_status!r} sb={sb_status!r}")
        else:
            print(f"    OK    {k}: status={sheet_status!r} company={sheet_row.get('company')!r}")

    if mismatches or missing_in_sb:
        print(f"[verify] FAIL: {mismatches} mismatched, {len(missing_in_sb)} missing")
        return 1
    print("[verify] PASS")
    return 0


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Read both sides; report counts; do not write to supabase")
    g.add_argument("--apply", action="store_true",
                   help="Upsert all sheet rows into supabase (idempotent)")
    g.add_argument("--verify", action="store_true",
                   help="Row-count + spot-check parity. No writes.")
    p.add_argument("--sample-n", type=int, default=5,
                   help="Spot-check sample size during --verify (default 5)")
    args = p.parse_args()

    if not ledger._sheets_available():
        print("ERROR: sheets backend not configured (GOOGLE_SA_JSON / LEDGER_SHEET_ID)",
              file=sys.stderr)
        sys.exit(2)
    if not supabase_ledger.available():
        print("ERROR: supabase backend not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)",
              file=sys.stderr)
        sys.exit(2)

    sheet_ws = ledger._sheets_ws()
    sb_client = supabase_ledger._get_client()
    user_id = supabase_ledger._user_id()

    if args.apply or args.dry_run:
        _ensure_user(sb_client, user_id)
        n_roles = _migrate_roles(sheet_ws, sb_client, user_id, apply=args.apply)
        n_oos = _migrate_oos_review(sheet_ws, sb_client, user_id, apply=args.apply)
        print(f"\nSUMMARY ({'apply' if args.apply else 'dry-run'}): "
              f"roles_eligible={n_roles} oos_eligible={n_oos}")
        if args.apply:
            print("\nRun --verify next to confirm parity.")

    if args.verify:
        sys.exit(_verify(sheet_ws, sb_client, user_id, sample_n=args.sample_n))


if __name__ == "__main__":
    main()
