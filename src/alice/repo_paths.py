"""Canonical repo-path resolution for the job-search ("Alice") project.

Single source of truth for the repo root and its common subdirectories, so no
module hardcodes ``<an absolute developer path>`` anymore.

Resolution order:
  1. ``JOB_SEARCH_ROOT`` env var, if set (lets the daemon/tests relocate).
  2. Otherwise the repo root inferred from this file's location
     (``src/alice/repo_paths.py`` -> repo root).

Everything is a ``pathlib.Path``. Subdir constants are derived from ROOT so a
single override moves the whole tree. ``feedback/`` is the local-only "state
repo" (see scripts/snapshot_state.sh) — its files are live diagnostic artifacts
the daemon writes; the FB_* constants below name the ones referenced in code.
"""
import os
from pathlib import Path

# Repo root: env override wins, else repo root inferred from this file's location.
ROOT = Path(os.environ.get("JOB_SEARCH_ROOT", Path(__file__).resolve().parents[2]))

# Top-level subdirectories.
SCRIPTS = ROOT / "scripts"
APPLICATIONS = ROOT / "applications"
TEMPLATES = ROOT / "templates"
DAILY = ROOT / "daily"
DOCS = ROOT / "docs"
SELF = ROOT / "self"
KNOWLEDGE = ROOT / "knowledge"
FEEDBACK = ROOT / "feedback"

# Commonly referenced top-level files.
ALICE_MD = ROOT / "Alice.md"
PIPELINE_DB = ROOT / "pipeline.db"

# feedback/ artifacts referenced from code (the local-only state repo).
FB_OBSERVATIONS = FEEDBACK / "observations.md"
FB_FOCUS = FEEDBACK / "focus.json"
FB_HYPOTHESES = FEEDBACK / "hypotheses.md"
FB_FULL_PROMPT_LAST = FEEDBACK / "full-prompt-last.txt"
FB_DEBUG_CONTEXT_LAST = FEEDBACK / "debug-context-last.txt"
FB_TIME_COST_LOG = FEEDBACK / "time-cost-log.jsonl"
FB_PENDING_CONFIRMATION = FEEDBACK / "pending-confirmation.json"
FB_SCHEDULED_SCREENS = FEEDBACK / "scheduled-screens.json"
FB_PREP_QUEUE = FEEDBACK / "prep-queue.json"
FB_DIGEST_PREFS = FEEDBACK / "digest-prefs.json"
FB_THREADS = FEEDBACK / "threads"
FB_SCORECARDS = FEEDBACK / "scorecards"
FB_PROPOSED = FEEDBACK / "proposed"
FB_TELEGRAM_HISTORY = FEEDBACK / "telegram-history.jsonl"
FB_SUPERSEDED_DIRECTIVES = FEEDBACK / "superseded-directives.jsonl"
FB_SHEET_WRITE_LOG = FEEDBACK / "sheet-write-log.jsonl"
FB_SHEET_WRITE_BLOCKED = FEEDBACK / "sheet-write-blocked.jsonl"
FB_QUESTION_ANSWERS = FEEDBACK / "question-answers.jsonl"
FB_PROGRESS_STATUS = FEEDBACK / "progress-status.log"
FB_OUTREACH_RESPONSES = FEEDBACK / "outreach-responses.jsonl"
FB_INTENT_WRITE_LOG = FEEDBACK / "intent-write-log.jsonl"
FB_DEBRIEF_QUEUE = FEEDBACK / "debrief-queue.json"
FB_DEBRIEF_ANSWERS = FEEDBACK / "debrief-answers.jsonl"
