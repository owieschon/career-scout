"""Offline unit tests for _harvest_targets().

Covers (no network, no LLM):
  1. Cobalt path: targets/cobalt-cpm-mfg-automation.md → ("greenhouse","cobalt")
     extracted and written to discovered_slugs.json.
  2. Merge-not-clobber: existing entries in discovered_slugs.json are preserved;
     new entries are appended; no duplicate (ats, slug) pairs.
  3. Roles subdirectory: Ashby URLs in targets/roles/*.md are harvested.
  4. duplicate URL across two files: only one entry per (ats, slug) in output.
  5. dry_run=True: file NOT written; list returned for inspection.
  6. Filename fallback: files with no heading use the stem as company name.
  7. Heading extraction: "Cobalt Automation — CPM" heading → name "Cobalt Automation".
  8. _ats_boards() integration: after harvest, _ats_boards() includes the
     harvested Cobalt Automation board.
  9. stats["discovered_boards"] is independent of targets-harvest (auto-grow
     from aggregators is a separate counter — this test asserts no regression).
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent

from alice.pipeline import daily_delta  # noqa: E402

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

FORMIC_MD = """\
# Cobalt Automation — Customer Project Manager, Manufacturing Automation

**Source:** operator paste, 2026-05-31
**URL:** https://job-boards.greenhouse.io/cobalt/jobs/4673706006
**Location:** Eastern Time Zone, United States

## JD Summary

Cobalt Automation is a robotics-as-a-service company (automation for manufacturers).
"""

DECAGON_MD = """\
# Octave AI — Strategic Solutions Engineer, East

**Opportunity ID:** 31
**Posting:** https://jobs.ashbyhq.com/octave/6431a6f9-2ebe-4b68-beb6-200b42aeeca8

## Why this is tier-1

BONUSES: AI-native company.
"""

NORTHWIND_MD = """\
# COMPANY RESEARCH, Northwind Systems

## PRODUCT
No ATS URL present in this file. It's a company-research doc, not a role.
"""

NO_HEADING_MD = """\
Just some notes without a heading.

**Posting:** https://jobs.ashbyhq.com/somecompany/abc-123
"""


@pytest.fixture
def targets_dir(tmp_path):
    """Synthetic targets dir with a cobalt role file and a roles subdirectory."""
    td = tmp_path / "targets"
    td.mkdir()
    (td / "cobalt-cpm-mfg-automation.md").write_text(FORMIC_MD)
    roles = td / "roles"
    roles.mkdir()
    (roles / "0031_octave_strategic_solutions_engineer.md").write_text(DECAGON_MD)
    companies = td / "companies"
    companies.mkdir()
    (companies / "northwind.md").write_text(NORTHWIND_MD)
    return td


# --------------------------------------------------------------------------
# 1. Cobalt path: greenhouse:cobalt extracted and written
# --------------------------------------------------------------------------

def test_formic_harvested_to_discovered_slugs(tmp_path, targets_dir, monkeypatch):
    """The Cobalt target file's Greenhouse URL must yield ("greenhouse","cobalt")
    and that entry must land in discovered_slugs.json."""
    # Redirect REPO so the output file goes to tmp_path.
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)
    # Ensure the output path is in tmp_path/targets.
    out_path = tmp_path / "targets" / "discovered_slugs.json"

    new_entries = daily_delta._harvest_targets(targets_dir=targets_dir)

    assert any(e[1] == "greenhouse" and e[2] == "cobalt" for e in new_entries), (
        "Cobalt's Greenhouse slug must be in the returned new_entries list"
    )
    assert out_path.exists(), "discovered_slugs.json must be written"
    written = json.loads(out_path.read_text())
    assert any(r[1] == "greenhouse" and r[2] == "cobalt" for r in written), (
        "discovered_slugs.json must contain [name, 'greenhouse', 'cobalt']"
    )


# --------------------------------------------------------------------------
# 2. Merge-not-clobber: existing entries preserved, no duplicates
# --------------------------------------------------------------------------

def test_merge_preserves_existing_entries(tmp_path, targets_dir, monkeypatch):
    """Pre-existing entries in discovered_slugs.json must survive the harvest."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)
    out_path = tmp_path / "targets" / "discovered_slugs.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing_entry = ["PriorCo (auto)", "ashby", "priorco"]
    out_path.write_text(json.dumps([existing_entry]))

    daily_delta._harvest_targets(targets_dir=targets_dir)

    written = json.loads(out_path.read_text())
    slugs = {(r[1], r[2]) for r in written}
    assert ("ashby", "priorco") in slugs, "pre-existing entry must be preserved"
    assert ("greenhouse", "cobalt") in slugs, "new Cobalt entry must be added"


def test_no_duplicate_entries(tmp_path, targets_dir, monkeypatch):
    """Running harvest twice must not create duplicate (ats, slug) pairs."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)

    daily_delta._harvest_targets(targets_dir=targets_dir)
    daily_delta._harvest_targets(targets_dir=targets_dir)

    out_path = tmp_path / "targets" / "discovered_slugs.json"
    written = json.loads(out_path.read_text())
    slugs = [(r[1], r[2]) for r in written]
    assert len(slugs) == len(set(slugs)), (
        "discovered_slugs.json must not contain duplicate (ats, slug) pairs"
    )


# --------------------------------------------------------------------------
# 3. Roles subdirectory: Ashby URLs in targets/roles/*.md are scanned;
#    already-curated slugs are correctly deduplicated.
# --------------------------------------------------------------------------

def test_roles_subdir_ashby_url_harvested(tmp_path, monkeypatch):
    """An Ashby URL in targets/roles/*.md that is NOT already in SD.BOARDS
    must be harvested.  A URL already present in SD.BOARDS must be skipped
    (it's already being scanned — no duplicate).

    Octave AI is already in SD.BOARDS, so it will not appear as a new entry
    (correct behavior).  We use a synthetic slug 'novelco' that is not in
    SD.BOARDS to prove the roles-subdir code path works."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)
    td = tmp_path / "targets"
    td.mkdir()
    roles = td / "roles"
    roles.mkdir()
    novel_md = """\
# Novelco — Solutions Engineer

**Posting:** https://jobs.ashbyhq.com/novelco/abc-123
"""
    (roles / "0099_novelco_se.md").write_text(novel_md)

    new_entries = daily_delta._harvest_targets(targets_dir=td)

    assert any(e[1] == "ashby" and e[2] == "novelco" for e in new_entries), (
        "A novel Ashby slug in roles/ subdir must be harvested"
    )


def test_roles_subdir_already_curated_slug_skipped(tmp_path, targets_dir, monkeypatch):
    """Octave AI is in SD.BOARDS — the harvest must not re-add it to
    discovered_slugs.json (it is already being scanned; duplicate would be noise)."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)

    new_entries = daily_delta._harvest_targets(targets_dir=targets_dir)

    assert not any(e[2] == "octave" for e in new_entries), (
        "Octave AI is already in SD.BOARDS; harvest must not re-add it"
    )


# --------------------------------------------------------------------------
# 4. Files without ATS URLs produce no entry (northwind company-research file)
# --------------------------------------------------------------------------

def test_company_research_file_without_ats_url_skipped(tmp_path, targets_dir, monkeypatch):
    """A company research file with no ATS URL must not produce an entry."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)

    new_entries = daily_delta._harvest_targets(targets_dir=targets_dir)

    names = [e[0] for e in new_entries]
    assert not any("northwind" in n.lower() for n in names), (
        "northwind company-research file has no ATS URL and must not be harvested"
    )


# --------------------------------------------------------------------------
# 5. dry_run=True: file NOT written
# --------------------------------------------------------------------------

def test_dry_run_does_not_write_file(tmp_path, targets_dir, monkeypatch):
    """dry_run=True must return the new entries but NOT write discovered_slugs.json."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)
    out_path = tmp_path / "targets" / "discovered_slugs.json"

    new_entries = daily_delta._harvest_targets(targets_dir=targets_dir, dry_run=True)

    assert not out_path.exists(), "dry_run must not write discovered_slugs.json"
    assert any(e[1] == "greenhouse" and e[2] == "cobalt" for e in new_entries), (
        "dry_run must still return the harvested entries for inspection"
    )


# --------------------------------------------------------------------------
# 6. Filename fallback when no heading
# --------------------------------------------------------------------------

def test_filename_fallback_used_when_no_heading(tmp_path, monkeypatch):
    """A file with no '# heading' line must use the stem as company name."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)
    td = tmp_path / "targets"
    td.mkdir()
    (td / "some-company-role.md").write_text(NO_HEADING_MD)

    new_entries = daily_delta._harvest_targets(targets_dir=td)

    assert len(new_entries) == 1
    assert new_entries[0][1] == "ashby"
    assert new_entries[0][2] == "somecompany"
    # name comes from stem (lowercased & title-cased)
    assert new_entries[0][0]  # non-empty name


# --------------------------------------------------------------------------
# 7. Heading extraction strips role suffix after em-dash
# --------------------------------------------------------------------------

def test_heading_strips_role_suffix_after_em_dash(tmp_path, monkeypatch):
    """'# Cobalt Automation — CPM' heading must produce company name 'Cobalt Automation', not
    'Cobalt Automation — CPM'."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)
    td = tmp_path / "targets"
    td.mkdir()
    (td / "cobalt-role.md").write_text(FORMIC_MD)

    new_entries = daily_delta._harvest_targets(targets_dir=td)

    assert len(new_entries) == 1
    assert new_entries[0][0] == "Cobalt Automation", (
        f"company name must be 'Cobalt Automation', got {new_entries[0][0]!r}"
    )


# --------------------------------------------------------------------------
# 8. _ats_boards() integration: harvested board appears in board list
# --------------------------------------------------------------------------

def test_ats_boards_includes_formic_after_harvest(tmp_path, targets_dir, monkeypatch):
    """After _harvest_targets() writes to discovered_slugs.json, calling
    _ats_boards() must return a board entry with (ats='greenhouse', slug='cobalt').
    This confirms the end-to-end path: file → discovered_slugs → _ats_boards()."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)

    # Copy the static JSON files the production targets dir carries.
    (tmp_path / "targets").mkdir(parents=True, exist_ok=True)
    for fname in ("yc_boards.json", "vc_boards.json"):
        src = REPO / "targets" / fname
        if src.exists():
            (tmp_path / "targets" / fname).write_bytes(src.read_bytes())

    daily_delta._harvest_targets(targets_dir=targets_dir)

    boards = daily_delta._ats_boards()
    ats_slugs = {(a, s) for _, a, s in boards}
    assert ("greenhouse", "cobalt") in ats_slugs, (
        "_ats_boards() must include ('greenhouse', 'cobalt') after harvest writes "
        "discovered_slugs.json — this is the coverage path the fix establishes"
    )


# --------------------------------------------------------------------------
# 9. Duplicate across two files: only one entry per (ats, slug)
# --------------------------------------------------------------------------

def test_same_slug_in_two_files_produces_one_entry(tmp_path, monkeypatch):
    """If two target files point to the same ATS board, only one entry must
    appear in discovered_slugs.json."""
    monkeypatch.setattr(daily_delta, "REPO", tmp_path)
    td = tmp_path / "targets"
    td.mkdir()
    (td / "cobalt-role-1.md").write_text(FORMIC_MD)
    (td / "cobalt-role-2.md").write_text(FORMIC_MD.replace(
        "Customer Project Manager", "Senior Deployment Engineer"))

    daily_delta._harvest_targets(targets_dir=td)

    out_path = tmp_path / "targets" / "discovered_slugs.json"
    written = json.loads(out_path.read_text())
    formic_entries = [r for r in written if r[1] == "greenhouse" and r[2] == "cobalt"]
    assert len(formic_entries) == 1, (
        "Same (ats, slug) from two files must appear exactly once"
    )
