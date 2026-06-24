"""Mobile-friendly ledger reformat (idempotent, re-runnable).

The pipeline ledger is 13 columns — painful on mobile (scroll to find the link).
This makes it tappable + compact WITHOUT reordering columns (ledger.py writes
status/date/job_key by HARDCODED column index G=7 / L=12 / J=10, so column
positions are critical — we only hyperlink, hide, and freeze):

  - role (col C) -> =HYPERLINK(url, role)  so a tap opens the JD (the mobile win).
    get_all_records still returns the role TEXT label, so the daemon's read/match
    is unaffected.
  - hide noise columns (surfaced_date A, source E, url I, job_key J,
    status_changed_date L) — positions unchanged, just hidden.
  - freeze row 1 + company/role (frozenColumnCount=3), bold the header.

Run after the cron adds rows to re-hyperlink the new plain-text roles. Backs up
the full sheet first. Read deps: src/alice/persistence/ledger.py (auth/sheet id).
"""
import json
import os
import sys
from alice import repo_paths

_HERE = os.path.dirname(os.path.abspath(__file__))

HIDE_COLS = [0, 4, 8, 9, 11]   # A surfaced_date, E source, I url, J job_key, L status_changed_date
ROLE_COL_A1 = "C"              # role (hyperlinked)


def _ws():
    from alice.persistence import ledger
    import gspread
    from google.oauth2.service_account import Credentials
    cfg = ledger.load()
    creds = Credentials.from_service_account_file(
        cfg["GOOGLE_SA_JSON"], scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(cfg["LEDGER_SHEET_ID"]).sheet1


def hyperlink_formula(url: str, role: str) -> str:
    """=HYPERLINK(url, role). role quotes are softened to single-quotes for the
    formula. Empty/non-http url -> plain role (no formula)."""
    if not url or not str(url).startswith("http"):
        return role
    return f'=HYPERLINK("{url}","{str(role).replace(chr(34), chr(39))}")'


def apply(ws=None, *, backup=True):
    ws = ws or _ws()
    rows = ws.get_all_records()
    if backup:
        os.makedirs(os.path.join(str(repo_paths.ROOT), "state"), exist_ok=True)
        bpath = os.path.join(str(repo_paths.ROOT), "state", "ledger_backup_latest.json")
        json.dump(ws.get_all_values(), open(bpath, "w"))
 # role hyperlinks (idempotent — re-writing the same formula is a no-op visually)
    cells = []
    for i, r in enumerate(rows):
        url = str(r.get("url") or "")
        if url.startswith("http"):
            cells.append({"range": f"{ROLE_COL_A1}{i+2}",
                          "values": [[hyperlink_formula(url, str(r.get("role") or ""))]]})
    if cells:
        ws.batch_update(cells, value_input_option="USER_ENTERED")
 # structure: hide noise cols, freeze row1 + company/role, bold header
    sid = ws.id
    reqs = [{"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
                "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}}
            for c in HIDE_COLS]
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 3}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})
    reqs.append({"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
        "fields": "userEnteredFormat.textFormat.bold"}})
    ws.spreadsheet.batch_update({"requests": reqs})
    return {"hyperlinked": len(cells), "hidden_cols": len(HIDE_COLS)}


if __name__ == "__main__":
    print(apply())
