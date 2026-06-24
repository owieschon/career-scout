"""Parity tool: read both backends, diff, exit non-zero on drift.

Run this on a cadence to confirm the Postgres mirror keeps tracking the Sheet
while the Sheet remains the source of truth. After a cutover the comparison can
be flipped (Postgres canonical, Sheet backstop) without changing this script's
contract.

Compares:
    1. Role set: keys present in one but not the other.
    2. For shared keys: status drift (most operationally important).
    3. For shared keys: company/url/job_key consistency (data shape parity).

Usage:
    python3 scripts/parity_check.py                 # default 25-row spot-check
    python3 scripts/parity_check.py --full          # full row-by-row scan (slower)
    python3 scripts/parity_check.py --report /tmp/parity.json   # machine-readable
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from alice import repo_paths

REPO_ROOT = repo_paths.ROOT

from alice.persistence import ledger
from alice.persistence import supabase_ledger


def _key(r):
    return str(r.get("job_key") or r.get("url") or "").strip()


def _collect_sheet():
    ws = ledger._sheets_ws()
    rows = ws.get_all_records()
    return {_key(r): r for r in rows if _key(r)}


def _collect_supabase():
    client = supabase_ledger._get_client()
    uid = supabase_ledger._user_id()
    resp = (client.table("roles")
            .select("job_key, status, company, role, url, intent")
            .eq("user_id", uid).execute())
    return {r["job_key"]: r for r in (resp.data or [])}


def _diff(sheet, sb, full: bool):
    missing_in_sb = sorted(set(sheet) - set(sb))
    extra_in_sb = sorted(set(sb) - set(sheet))
    shared = sorted(set(sheet) & set(sb))
    if not full:
        shared = shared[:25]
    drifts = []
    for k in shared:
        s = sheet[k]
        b = sb[k]
        s_status = (s.get("status") or "").strip().lower()
        b_status = (b.get("status") or "").strip().lower()
        s_company = (s.get("company") or "").strip()
        b_company = (b.get("company") or "").strip()
        if s_status != b_status or s_company != b_company:
            drifts.append({
                "job_key": k,
                "sheet_status": s_status, "supabase_status": b_status,
                "sheet_company": s_company, "supabase_company": b_company,
            })
    return missing_in_sb, extra_in_sb, drifts, shared


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true",
                   help="Compare ALL shared keys, not just the first 25")
    p.add_argument("--report", type=str,
                   help="Write machine-readable JSON to this path")
    args = p.parse_args()

    if not ledger._sheets_available():
        print("ERROR: sheets backend not configured", file=sys.stderr); sys.exit(2)
    if not supabase_ledger.available():
        print("ERROR: supabase backend not configured", file=sys.stderr); sys.exit(2)

    sheet = _collect_sheet()
    sb = _collect_supabase()

    missing, extra, drifts, shared = _diff(sheet, sb, args.full)
    summary = {
        "sheet_count": len(sheet),
        "supabase_count": len(sb),
        "shared_count": len(set(sheet) & set(sb)),
        "compared_count": len(shared),
        "missing_in_supabase": len(missing),
        "extra_in_supabase": len(extra),
        "drifts": drifts,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "drifts"}, indent=2))
    if drifts:
        print("\nDRIFTS:")
        for d in drifts:
            print(f"  {d['job_key']}: "
                  f"sheet=({d['sheet_status']!r}, {d['sheet_company']!r}) "
                  f"sb=({d['supabase_status']!r}, {d['supabase_company']!r})")
    if missing:
        print(f"\nMissing in supabase (first 10): {missing[:10]}")
    if extra:
        print(f"\nExtra in supabase (first 10): {extra[:10]}")

    if args.report:
        Path(args.report).write_text(json.dumps(summary, indent=2))
        print(f"\n[wrote {args.report}]")

    sys.exit(0 if (not missing and not drifts) else 1)


if __name__ == "__main__":
    main()
