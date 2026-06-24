"""Negotiation prep — triggered when status moves to 'offer'.

Reads:  sheet for newly-'offer' rows without applications/<slug>/negotiation-prep.md
Writes: applications/<slug>/negotiation-prep.md
"""
import re
import sys
from datetime import datetime
from pathlib import Path

from alice import repo_paths
from alice.llm import llm
from alice.persistence import ledger

APPS_DIR = Path(repo_paths.APPLICATIONS)


def _slug(company, role):
    s = f"{company}-{role}".lower()
    s = re.sub(r"[^a-z0-9-]+", "-", s)
    return re.sub(r"-+", "-", s).strip("-")[:80]


def _newly_offered():
    ws = ledger._ws()
    rows = ws.get_all_records()
    out = []
    for i, r in enumerate(rows, start=2):
        s = (r.get("status") or "").strip().lower()
        if s not in ("offer", "negotiating"):
            continue
        company = r.get("company", "")
        role = r.get("role", "")
        slug = _slug(company, role)
        pkg_dir = APPS_DIR / slug
        if (pkg_dir / "negotiation-prep.md").exists():
            continue
        out.append({"row_idx": i, "row": r, "slug": slug, "pkg_dir": pkg_dir})
    return out


def generate_negotiation(row, pkg_dir):
    company = row.get("company", "")
    role = row.get("role", "")
    comp_disclosed = row.get("comp", "n/d")
    url = row.get("url", "")
    pkg_dir.mkdir(parents=True, exist_ok=True)

    brief = llm.load_alice_brief()
    prompt = f"""The operator just received an offer at {company} for the {role} role.
Disclosed comp band in the listing: {comp_disclosed}
URL: {url}

Produce `negotiation-prep.md` per your brief (Triggered by `offer` section). Sections:

1. **Comp benchmarking** — name comparable comp for this role/level/company stage from public sources you know (levels.fyi-pattern data, recent funding context, market band). Be honest about what you can confidently estimate vs what the operator should verify. The operator's target band is an example band (e.g. $150k–$190k base).

2. **Components to evaluate** — base, equity (% / vesting / strike / refresh policy / liquidation preferences if applicable), bonus, sign-on, RSUs vs options, benefits, severance terms, IP assignment language, non-compete, remote-work guarantee (operator's lock — confirm in writing).

3. **What to ask for beyond base** — specific asks ranked by company-context likelihood. Examples: equity bumps for sub-Series-B; sign-on bonuses for Series-C+; accelerated review cycles; faster equity-vesting cliffs.

4. **Counter language drafts** — 3-4 sentences the operator can send back. Direct, not adversarial. Frame as collaborative alignment, not demand. Operator's voice: no em dashes, no "passionate", no consulting-speak.

5. **Multi-offer leverage** — if the operator mentions a parallel conversation, how to message timing without burning bridges. Even if no parallel, draft the optional-mention version.

6. **Decision framework** — not just "is the comp good" but "is this the right next role for the 2-year arc; does it solve runway AND trajectory; what's the optionality cost."

7. **Negotiation tree** — when to push, when to accept, when to walk. Conditional language: "If they hold firm on base, ask for X. If they hold on equity, ask for Y."

The operator's full offer details aren't in this brief; ask in the conclusion to paste the actual offer
terms via 'offer details: <text>' directive so you can produce a sharper counter on the next pass.
"""
    print(f"    drafting negotiation prep (model: {llm.MODEL_FOR_TASK['negotiation_prep']})...")
    res = llm.call("negotiation_prep", prompt, system=brief, max_tokens=3000)
    path = pkg_dir / "negotiation-prep.md"
    header = f"""# Negotiation Prep — {company} · {role}

**Generated:** {datetime.now().isoformat(timespec='seconds')}
**Disclosed comp:** {comp_disclosed}
**LLM:** {res['model']} (${res['cost_usd']:.4f})

---

"""
    path.write_text(header + res["text"])
    return {"path": str(path), "cost": res["cost_usd"]}


def main():
    newly = _newly_offered()
    if not newly:
        print("[negotiation: no offers awaiting prep]")
        try:
            from alice.persistence import activity_log
            activity_log.record(step="negotiation_prep",
                                summary="no offers awaiting prep",
                                count=0, status="noop")
        except Exception as e:
            print(f"[activity_log: {e}]")
        return
    total = 0.0
    prepped = []
    for entry in newly:
        co = entry['row'].get('company')
        print(f"  [negotiation: prep for {co} - {entry['row'].get('role')}]")
        try:
            res = generate_negotiation(entry["row"], entry["pkg_dir"])
            total += res["cost"]
            prepped.append(co)
            print(f"    wrote {res['path']} (${res['cost']:.4f})")
        except Exception as e:
            print(f"    ERROR: {e}")
    print()
    print(f"summary: {len(newly)} negotiation prep packages drafted, ${total:.4f} spent")
    try:
        from alice.persistence import activity_log
        activity_log.record(
            step="negotiation_prep",
            summary=f"{len(prepped)} negotiation pack{'' if len(prepped) == 1 else 's'} prepped ({', '.join(prepped)})",
            count=len(prepped), cost=total,
            details={"companies": prepped},
        )
    except Exception as e:
        print(f"[activity_log: {e}]")


if __name__ == "__main__":
    main()
