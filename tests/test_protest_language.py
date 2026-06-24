"""Integrity gate: the prep writer must not re-introduce lone-wolf / defensive /
overclaim filler ("solo", "single-handedly", "personally", "not just a...",
"strongest/world-class") at GENERATION time. Cleaning the primitives corpus fixes
the input; this gate catches the WRITER composing fresh prose with the same
mistake. Warning-level (flag-to-human, no withhold). Pure (no LLM).

The key discrimination this locks: confidence-PROTESTING negation is flagged;
honest claim-SCOPING negation ("schema literacy, not a built integration") is NOT
— scoping prevents over-claim and must survive."""
from pathlib import Path

from alice.pipeline import prep_pipeline as p


PROTEST = [
    "Built the platform solo.",
    "Single-handedly shipped production ML.",
    "I personally architected the system.",
    "Not just a builder, a true operator.",
    "The strongest available evidence of agentic engineering.",
    "A world-class, cutting-edge platform.",
]
CLEAN = [
    # honest scoping negation — must NOT fire (it guards against over-claim)
    "ERP schema literacy, not a built integration.",
    "This is data-and-schema literacy, not a claim of having integrated those ERPs.",
    # confident, hands-on framing — the desired voice
    "Owned the full stack end to end, hands-on across ML, agents, and UI.",
    "Built and shipped two production agent systems.",
    # word-boundary false-positive guards
    "Built a standalone Cloudflare Worker.",
    "Designed the personalization layer for reps.",  # 'personal' substring, not 'personally'
    # possessive 'my own <noun>' — must NOT fire
    "Built guardrails on my own agents with a fail-loud action gate.",
    "Tested it against my own production data.",
]


def test_flags_protest_language():
    for text in PROTEST:
        assert p._protest_language_hits(text), f"should be flagged: {text!r}"


def test_allows_scoping_and_clean_voice():
    for text in CLEAN:
        assert not p._protest_language_hits(text), f"should NOT be flagged: {text!r}"


def test_verify_counts_protest_warnings():
    """The detector is wired into stage_verify as a soft warning counter, not a
    withhold — mirrors value_led_warnings (surfaced to the operator, drafts still ship)."""
    hits = p._protest_language_hits("Built it solo; the strongest evidence of skill.")
    assert len(hits) >= 2
    assert all(h["type"] == "protest_language" for h in hits)
