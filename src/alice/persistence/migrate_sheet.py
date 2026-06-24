"""One-time sheet schema migration to support Alice round 1.

Changes:
  - Add column L: status_changed_date (ISO date when status last changed)
  - Expand status dropdown to: new | good fit | not a fit | materials pending |
    submitted | first screen scheduled | interviewing | offer | negotiating | closed

Idempotent — safe to re-run; checks existing state before changing anything.
"""
import sys
from pathlib import Path

from alice.persistence import ledger

NEW_HEADERS = ["surfaced_date", "company", "role", "comp", "source", "score",
               "status", "notes", "url", "job_key", "rationale", "status_changed_date"]

NEW_STATUSES = ["new", "good fit", "not a fit", "materials pending", "submitted",
                "first screen scheduled", "interviewing", "offer", "negotiating", "closed"]


def main():
    ws = ledger._ws()
    print(f"sheet: {ws.title} ({ws.row_count} rows × {ws.col_count} cols)")

 # 1) Ensure column L exists with the right header
    current_l = (ws.acell("L1").value or "").strip()
    if current_l == "status_changed_date":
        print("  L1 header: already 'status_changed_date'")
    else:
        if ws.col_count < 12:
            ws.add_cols(12 - ws.col_count)
            print(f"  added cols up to 12 (was {ws.col_count})")
        ws.update("L1", [["status_changed_date"]])
        print("  L1 header: set to 'status_changed_date'")

 # 2) Backfill column L for any rows with status set but no date — use today's date as floor
    from datetime import date
    today = date.today().isoformat()
    rows = ws.get_all_records()
    backfill_cells = []
    from gspread.utils import rowcol_to_a1
    for idx, r in enumerate(rows, start=2):
        status = (r.get("status") or "").strip().lower()
        existing_date = (r.get("status_changed_date") or "").strip()
 # only backfill rows that have a non-default status but no date yet
        if status and status != "new" and not existing_date:
            backfill_cells.append({"range": rowcol_to_a1(idx, 12), "values": [[today]]})
    if backfill_cells:
        ws.batch_update(backfill_cells, value_input_option="RAW")
        print(f"  backfilled status_changed_date on {len(backfill_cells)} rows (using today as floor)")
    else:
        print("  no rows needed backfill")

 # 3) Expand the status dropdown
    ws.spreadsheet.batch_update({"requests": [{
        "setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 2000,
                      "startColumnIndex": 6, "endColumnIndex": 7},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": s} for s in NEW_STATUSES]},
                     "showCustomUi": True, "strict": False}}}]})
    print(f"  expanded status dropdown to {len(NEW_STATUSES)} options")

    print()
    print("migration complete.")


if __name__ == "__main__":
    main()
