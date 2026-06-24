"""Programmatic constraint checks for Alice's outputs.

Run on every generated artifact (resume drafts, cover letters, outreach, digests, etc.)
to catch voice/PII violations BEFORE they go to the operator.

Returns a list of violations. Empty list = clean.

Usage:
    from alice.harness.constraints import check
    violations = check(alice_output_text)
    if violations:
        # surface or block depending on severity
"""
import re

# Hard voice constraints from Alice's brief + the operator's voice rules
BANNED_WORDS = [
    # consulting-speak
    "passionate", "leveraged", "leverage", "synergies", "synergy",
    "deliver value", "value-add", "value add", "best-in-class",
    "best in class", "circle back", "touch base", "deep dive into",
    "low-hanging fruit", "move the needle", "boil the ocean",
    "thought leadership", "thought leader", "trusted advisor",
    "drive value", "drive results", "drive impact",
    # generic praise
    "exciting opportunity", "great fit for me", "incredibly excited",
    "delighted to apply", "honored to apply", "thrilled to apply",
]

# Em dash check (the operator's voice rule forbids them).
# Includes both Unicode em dash (—) and double-hyphen approximations (--)
# used as dashes (not the legitimate "command --flag" pattern).
EM_DASH_RE = re.compile(r"[—–]")
# Double-hyphen used as dash (between words, with surrounding spaces or punctuation):
DOUBLE_HYPHEN_AS_DASH_RE = re.compile(r"(?<=\w)\s*--\s*(?=\w)")

# PII patterns — Alice should never emit these even when the operator's data
# contains them. These are generic structural patterns (contact details that
# should never appear in an outbound artifact).
PII_PATTERNS = [
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "phone number"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "SSN-format"),
    (re.compile(r"\b\d{1,5}\s+[\w\s]{3,30}\s+(St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Blvd|Ln|Lane|Ct|Court)\b", re.I), "street address"),
]


def check(text, severity_threshold="warn"):
    """Scan text for constraint violations.
    Returns list of dicts: {type, severity, match, position, message}.
    Severity levels: 'error' (block), 'warn' (surface to the operator), 'info' (log only)."""
    if not text:
        return []
    violations = []

    # Em dashes (error; the operator's locked voice rule)
    for m in EM_DASH_RE.finditer(text):
        violations.append({
            "type":     "em_dash",
            "severity": "error",
            "match":    m.group(0),
            "position": m.start(),
            "context":  text[max(0, m.start() - 30):m.end() + 30],
            "message":  "em dash used; the operator's voice rule forbids them",
        })
    for m in DOUBLE_HYPHEN_AS_DASH_RE.finditer(text):
        violations.append({
            "type":     "double_hyphen_dash",
            "severity": "error",
            "match":    m.group(0),
            "position": m.start(),
            "context":  text[max(0, m.start() - 30):m.end() + 30],
            "message":  "double hyphen used as dash; the operator's voice rule forbids dash usage",
        })

    # Banned words (warn — most are consulting-speak that's banned but not catastrophic)
    text_lower = text.lower()
    for word in BANNED_WORDS:
        idx = 0
        while True:
            pos = text_lower.find(word, idx)
            if pos < 0:
                break
            # word-boundary check
            before_ok = pos == 0 or not text[pos - 1].isalnum()
            after_pos = pos + len(word)
            after_ok = after_pos >= len(text) or not text[after_pos].isalnum()
            if before_ok and after_ok:
                violations.append({
                    "type":     "banned_word",
                    "severity": "warn",
                    "match":    text[pos:after_pos],
                    "position": pos,
                    "context":  text[max(0, pos - 30):after_pos + 30],
                    "message":  f"'{word}' is banned per voice rules",
                })
            idx = after_pos

    # PII (error — never emit)
    for pat, label in PII_PATTERNS:
        for m in pat.finditer(text):
            violations.append({
                "type":     "pii",
                "severity": "error",
                "match":    m.group(0),
                "position": m.start(),
                "context":  text[max(0, m.start() - 30):m.end() + 30],
                "message":  f"PII pattern matched: {label}",
            })

    # Filter by severity threshold
    threshold_rank = {"info": 0, "warn": 1, "error": 2}
    keep = threshold_rank.get(severity_threshold, 1)
    return [v for v in violations if threshold_rank.get(v["severity"], 0) >= keep]


def check_file(path, severity_threshold="warn"):
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return [{"type": "missing_file", "severity": "error", "message": f"file not found: {path}"}]
    return check(p.read_text(), severity_threshold)


def format_violations(violations):
    if not violations:
        return "  (clean)"
    out = []
    for v in violations:
        out.append(f"  [{v['severity']}] {v['type']}: {v['message']}")
        if v.get("context"):
            out.append(f"    context: ...{v['context']}...")
    return "\n".join(out)


if __name__ == "__main__":
    # Self-test. Synthetic persona only; the literals below exist to trip the
    # banned-word, em-dash, and structural-PII (phone/street) checks.
    test = """This is a passionate professional who has leveraged synergies — and his work has driven value at scale.
He is excited to apply to roles that fit his trajectory.
Jordan Avery lives at 123 Example St, phone (614) 555-0100, email jordan.avery@example.com.
"""
    print("Test input:")
    print(test)
    print()
    print("Violations:")
    print(format_violations(check(test, severity_threshold="info")))
