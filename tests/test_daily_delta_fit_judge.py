"""Unit tests for the fit-judge key-mapping adapter in daily_delta.

The acceptance bar: "no None body reaches the judge." The silent failure these
tests pin down is the key mismatch -- survivor recs carry source-schema keys
(desc/base_low/base_high/ext_id) while fit_judge.judge_survivors() reads
body/comp_low/comp_high/id. A bare call would judge every role against the
"(no JD body available)" fallback and emit garbage verdicts with no error.

Mirrors tests/test_fit_judge.py: insert scripts/ on sys.path first.
"""
import sys
from pathlib import Path


from alice.pipeline import daily_delta  # noqa: E402


def _daily_shaped_rec(desc="On-domain robotics PM JD body, no travel.", **over):
    """A survivor rec shaped exactly like new_qualified.append() builds in
    daily_delta.run() -- source keys, not judge keys."""
    rec = {
        "source": "ats", "ext_id": "co-123", "company": "Trailhead Robotics",
        "title": "Product Manager", "url": "http://example/x", "desc": desc,
        "location": "Columbus, OH", "base_low": 150000, "base_high": 190000,
        "remote_flag": False, "score": 88, "tier": "tier1",
        "comp": "$150,000-190,000",
    }
    rec.update(over)
    return rec


def test_adapter_maps_keys_no_none_body():
    """The core guarantee: a body-carrying survivor gets body/comp/id keys the
    judge reads, and body is the JD desc -- never None."""
    r = _daily_shaped_rec()
    judgeable, unjudged = daily_delta._prepare_fit_judge([r])
    assert unjudged == []
    assert judgeable == [r]
    assert r["body"] == r["desc"] and r["body"] is not None
    assert r["comp_low"] == r["base_low"]
    assert r["comp_high"] == r["base_high"]
    assert r["id"] == r["ext_id"]


def test_adapter_preserves_original_keys_for_output():
    """_write_output / ledger read score/company/title/comp/source/url off the
    same rec -- the adapter must not clobber them."""
    r = _daily_shaped_rec()
    daily_delta._prepare_fit_judge([r])
    for k in ("score", "company", "title", "comp", "source", "url"):
        assert k in r, f"adapter dropped {k!r} needed by _write_output"


def test_adapter_bodyless_is_loud_and_unjudged():
    """Empty desc -> not judged against the fallback; annotated loudly instead."""
    r = _daily_shaped_rec(desc="")
    judgeable, unjudged = daily_delta._prepare_fit_judge([r])
    assert judgeable == []
    assert unjudged == [r]
    assert r["fit_verdict"] == "UNJUDGED-NO-BODY"
    assert r["driving_constraint"] == "no_body"


def test_adapter_missing_desc_key_never_reaches_judge():
    """The exact silent-failure shape: a rec with NO desc key. It must end up
    body=None AND excluded from the judgeable set (never sent to the judge)."""
    r = _daily_shaped_rec()
    del r["desc"]
    judgeable, unjudged = daily_delta._prepare_fit_judge([r])
    assert judgeable == []
    assert unjudged == [r]
    assert r["body"] is None
    assert r["fit_verdict"] == "UNJUDGED-NO-BODY"


def test_adapter_partitions_mixed_batch():
    """Mixed batch: bodied recs are judgeable, body-less are quarantined, count
    is conserved (nothing silently lost)."""
    good = _daily_shaped_rec(ext_id="g1")
    bad = _daily_shaped_rec(ext_id="b1", desc="")
    judgeable, unjudged = daily_delta._prepare_fit_judge([good, bad])
    assert judgeable == [good]
    assert unjudged == [bad]
    assert len(judgeable) + len(unjudged) == 2
