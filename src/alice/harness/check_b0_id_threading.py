"""B0 (id threading) regression: run/session/job ids thread onto the llm.call
span + the cost-log record, and the redaction allow-list passes them through
untouched. Offline + hermetic — tests the PLUMBING, not a live trace. The live
end-to-end (a real call producing a stamped JSONL line + Phoenix span) is HELD
until tracing is enabled live (operator-greenlight)."""
import os
import sys
import inspect
from pathlib import Path
from alice.observability import telemetry
from alice.llm import llm


def test_id_attrs_pass_through_redaction_allowlist():
    # The ids are structured values we own — they must NOT be scrubbed/capped.
    for attr in ("session.id", "alice.run_id", "alice.job_key"):
        assert telemetry.redact(attr, "abc-123") == "abc-123", f"{attr} was scrubbed"


def test_trace_ids_collects_env_run_id_plus_params():
    os.environ["ALICE_RUN_ID"] = "20260531T101010-deadbe"
    try:
        ids = llm._trace_ids(session_id="chat-42", job_key="greenhouse:arize:123")
        assert ids == {"run_id": "20260531T101010-deadbe",
                       "session_id": "chat-42",
                       "job_key": "greenhouse:arize:123"}, ids
    finally:
        os.environ.pop("ALICE_RUN_ID", None)


def test_trace_ids_omits_blanks():
    os.environ.pop("ALICE_RUN_ID", None)
    assert llm._trace_ids() == {}
    assert llm._trace_ids(session_id="x") == {"session_id": "x"}
    assert llm._trace_ids(job_key="k") == {"job_key": "k"}


def test_call_signature_accepts_session_and_job_key():
    p = inspect.signature(llm.call).parameters
    assert "session_id" in p and "job_key" in p
