"""Reply directive parsing — beyond simple status updates.

Recognized directives (case-insensitive, leading whitespace tolerated):

  focus: A, B, C                  (replace focus list; comma-separated substrings)
  focus add: A                    (append to focus list)
  focus drop: A                   (remove from focus list)
  focus clear                     (empty focus list)
  focus show                      (request focus list shown in next digest)

  prep: A                         (start application package for A)
  prep order: A, B, C             (queue multiple)
  prep stop: A                    (halt work on A; leaves partial drafts)
  prep now: A                     (rush this one)

  screen scheduled: A, <when>, <interviewers>
                                  (sets status to 'first screen scheduled', records date)

  debrief: A                      (request debrief capture prompt)
  debrief now: A                  (immediate debrief request)
  debrief 1: <text>               (answer debrief prompt 1, 2, 3...)
  debrief r2: A                   (request debrief for round 2)

  answer 1: <text>                (answer to targeted question 1, 2, 3...)
                                  (Alice tracks pending question packages by app)

  hypothesis: <text>              (add to Alice's hypothesis registry)

  approve <id>                    (approve a pending proposal)
  reject <id>                     (reject a pending proposal)
  revert <id>                     (undo an auto-applied change)

  pause digest                    (stop daily digest until 'resume digest')
  lighter digest                  (reduce digest volume)
  resume digest                   (resume normal digest)

  help with <substring>           (Alice surfaces what's blocking on this role)
  revise <substring>              (ask for another pass on materials)
  drop <substring>                (drop a role — sheet status to 'not a fit')

  response from <name> at <company>: <positive|negative|declined|no-response|hostile>
                                  (outreach response tagging — feeds pattern tracking)

Returns a list of (directive_type, payload_dict) tuples for each matched line.
Lines that match NEITHER a directive NOR a status command go to the observations log.
"""
import re

from alice.pipeline import operator_intent

# Pattern definitions — order matters (longer/more-specific first).
# Each pattern returns (type, payload) where payload is a parsed dict.

_PATTERNS = [
 # Intent declarations: active/deliberating/holding/waiting/done: <role>.
 # Uses operator_intent.INTENT_DECL_RE — the same shared classifier the chat
 # path consumes, so there is one intent reader, not two.
    (operator_intent.INTENT_DECL_RE,
     lambda m: ("set_intent", {"intent": m.group(1).lower(), "substr": m.group(2).strip()})),

 # Focus directives
    (re.compile(r"^\s*focus\s+show\s*$", re.I),
     lambda m: ("focus_show", {})),
    (re.compile(r"^\s*focus\s+clear\s*$", re.I),
     lambda m: ("focus_clear", {})),
    (re.compile(r"^\s*focus\s+add\s*[:=]\s*(.+)$", re.I),
     lambda m: ("focus_add", {"substr": m.group(1).strip()})),
    (re.compile(r"^\s*focus\s+drop\s*[:=]\s*(.+)$", re.I),
     lambda m: ("focus_drop", {"substr": m.group(1).strip()})),
    (re.compile(r"^\s*focus\s*[:=]\s*(.+)$", re.I),
     lambda m: ("focus_set", {"substrings": [s.strip() for s in m.group(1).split(",") if s.strip()]})),

 # Prep directives
    (re.compile(r"^\s*prep\s+order\s*[:=]\s*(.+)$", re.I),
     lambda m: ("prep_order", {"substrings": [s.strip() for s in m.group(1).split(",") if s.strip()]})),
    (re.compile(r"^\s*prep\s+stop\s*[:=]\s*(.+)$", re.I),
     lambda m: ("prep_stop", {"substr": m.group(1).strip()})),
    (re.compile(r"^\s*prep\s+now\s*[:=]\s*(.+)$", re.I),
     lambda m: ("prep_now", {"substr": m.group(1).strip()})),
    (re.compile(r"^\s*prep\s*[:=]\s*(.+)$", re.I),
     lambda m: ("prep", {"substr": m.group(1).strip()})),

 # Screen scheduling
    (re.compile(r"^\s*screen\s+scheduled\s*[:=]\s*(.+)$", re.I),
     lambda m: ("screen_scheduled", _parse_screen_scheduled(m.group(1)))),

 # Debrief
    (re.compile(r"^\s*debrief\s+now\s*[:=]\s*(.+)$", re.I),
     lambda m: ("debrief_now", {"substr": m.group(1).strip()})),
    (re.compile(r"^\s*debrief\s+r(\d+)\s*[:=]\s*(.+)$", re.I),
     lambda m: ("debrief_request", {"substr": m.group(2).strip(), "round": int(m.group(1))})),
    (re.compile(r"^\s*debrief\s+(\d+)\s*[:=]\s*(.+)$", re.I),
     lambda m: ("debrief_answer", {"q_num": int(m.group(1)), "text": m.group(2).strip()})),
    (re.compile(r"^\s*debrief\s*[:=]\s*(.+)$", re.I),
     lambda m: ("debrief_request", {"substr": m.group(1).strip(), "round": 1})),

 # Targeted question answers
    (re.compile(r"^\s*answer\s+(\d+)\s*[:=]\s*(.+)$", re.I),
     lambda m: ("question_answer", {"q_num": int(m.group(1)), "text": m.group(2).strip()})),

 # Hypothesis
    (re.compile(r"^\s*hypothesis\s*[:=]\s*(.+)$", re.I),
     lambda m: ("hypothesis", {"text": m.group(1).strip()})),

 # Approve / reject / revert
    (re.compile(r"^\s*approve\s+(\S+)\s*$", re.I),
     lambda m: ("approve", {"id": m.group(1).strip()})),
    (re.compile(r"^\s*reject\s+(\S+)(?:\s*[:=]\s*(.+))?\s*$", re.I),
     lambda m: ("reject", {"id": m.group(1).strip(), "reason": (m.group(2) or "").strip() or None})),
    (re.compile(r"^\s*revert\s+(\S+)\s*$", re.I),
     lambda m: ("revert", {"id": m.group(1).strip()})),

 # Digest cadence control
    (re.compile(r"^\s*pause\s+digest\s*$", re.I),
     lambda m: ("digest_pause", {})),
    (re.compile(r"^\s*lighter\s+digest\s*$", re.I),
     lambda m: ("digest_lighter", {})),
    (re.compile(r"^\s*resume\s+digest\s*$", re.I),
     lambda m: ("digest_resume", {})),

 # Focus unsticking helpers
    (re.compile(r"^\s*help\s+with\s+(.+)$", re.I),
     lambda m: ("help_with", {"substr": m.group(1).strip()})),
    (re.compile(r"^\s*revise\s+(.+)$", re.I),
     lambda m: ("revise", {"substr": m.group(1).strip()})),
    (re.compile(r"^\s*drop\s+(.+)$", re.I),
     lambda m: ("drop_role", {"substr": m.group(1).strip()})),

 # Outreach response tagging
    (re.compile(r"^\s*response\s+from\s+(.+?)\s+at\s+(.+?)\s*[:=]\s*(positive|negative|declined|no-response|hostile)\s*$", re.I),
     lambda m: ("outreach_response", {
         "contact": m.group(1).strip(),
         "company": m.group(2).strip(),
         "classification": m.group(3).strip().lower(),
     })),
]


def _parse_screen_scheduled(payload):
    """Parse 'northwind enterprise, fri 6/14 11am ET, Alex Rivera + Sam Lee' into structured form."""
    parts = [p.strip() for p in payload.split(",", 2)]
    return {
        "substr":       parts[0] if len(parts) >= 1 else "",
        "when":         parts[1] if len(parts) >= 2 else "",
        "interviewers": parts[2] if len(parts) >= 3 else "",
    }


def parse_line(line):
    """Try each directive pattern. Return (type, payload) on first match, or (None, None)."""
    for pat, builder in _PATTERNS:
        m = pat.match(line)
        if m:
            return builder(m)
    return None, None


def parse_block(text):
    """Parse a full text block (e.g., post-strip reply body) into a list of (type, payload, raw_line)."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        t, p = parse_line(stripped)
        if t:
            out.append((t, p, stripped))
    return out


if __name__ == "__main__":
 # Self-test on a representative reply
    sample = """focus: northwind enterprise, watershed, dbt onboarding
prep: northwind enterprise
screen scheduled: northwind, fri 6/14 11am ET, Alex Rivera + Sam Lee
debrief 3: I felt strong on the Lattice Additive enterprise story.
answer 5: Yes, I led the Cadence Analytics migration of ~200K invoices.
hypothesis: LinkedIn DMs convert faster than cold email.
approve proposal-3
revert patch-2026-05-28-1
pause digest
help with northwind
response from Alex Rivera at Northwind: positive
this line is just observational chatter and should not parse"""
    for t, p, raw in parse_block(sample):
        print(f"  [{t}] {p}")
        print(f"    raw: {raw}")
