"""Write title-derived fit hypotheses for rows whose ATS can't be fetched programmatically.
Reasons from company + role title + known company-domain mappings — NOT generic praise."""
import sys
from pathlib import Path
from alice.persistence import ledger
from gspread.utils import rowcol_to_a1

MANUAL = {
 # (company, role-key-fragment) hypothesis text

 # SYNTHETIC EXAMPLE LIBRARY. These entries demonstrate the manual-enrichment
 # mechanism: a small hand-curated dict of (company, role-fragment) reasoned
 # fit hypothesis, used for rows whose ATS body can't be fetched programmatically.
 # The companies and reasoning below are fictional, illustrating the prose shape.
    ("Boreal CAD", "Strategic Account Executive"): """FIT HYPOTHESIS — Boreal CAD · Strategic Account Executive, FlowCAD
  1. FlowCAD is cloud-native CAD — the most credible product in the Boreal CAD portfolio to sell into modern engineering teams. Your engineering-software reseller experience is the exact buyer motion (legacy-CAD displacement is FlowCAD's top use case).
  2. Strategic AE motion (enterprise engineering org as buyer, multi-stakeholder, IT + Eng + Procurement) matches your Lattice Additive book at aerospace OEMs — same buying-committee shape.
  3. FlowCAD's primary objection is workflow migration risk; you've sat across the table from engineering managers weighing exactly that decision (desktop→cloud, traditional→additive).
  4. RISK: Boreal CAD's posting location was region-restricted before scrubbing. Verify role is truly remote-anywhere-US. Also confirm remote-first posture; enterprise AE at CAD/PLM vendors historically had on-site QBR expectations."""
,
    ("Cresta Analytics", "GTM Engineer"): """FIT HYPOTHESIS — Cresta Analytics · GTM Engineer
  1. Cresta Analytics focuses on industrial/manufacturing AI applications (asset performance, manufacturing intelligence) — directly on-domain with your Lattice Additive / Ironclad Industrial track and adjacent to Cadence Analytics's domain reasoning.
  2. GTM Engineer is the Forward-Deployed/RevOps hybrid that maps your operator-builder profile — you have both the technical depth (Cadence Analytics production ML) and the commercial fluency (years of B2B AM/CS) most GTM Eng candidates split.
  3. Industrial-AI buyers (plant managers, asset directors, COOs) speak in downtime/yield/OEE — vocabulary you used in Lattice Additive ROI conversations with aerospace OEMs and defense primes.
  4. RISK: Cresta Analytics's job posting is on iCIMS — couldn't read the JD body programmatically. Worth a manual read before applying to confirm comp and remote-eligibility."""
,
    ("Forge Parts", "Account Executive"): """FIT HYPOTHESIS — Forge Parts · Account Executive (Remote-US)
  1. Forge Parts is an on-demand manufacturing marketplace (CNC, sheet metal, 3D printing) — direct on-domain bullseye. Production-hardware + Lattice Additive + engineering-software experience gives you both the supplier-side AND buyer-side fluency Forge Parts's AEs need.
  2. Buyer profile (engineers/procurement at hardware companies sourcing custom parts) is exactly the audience you sold Lattice Additive metal AM to — same call.
  3. AE at a marketplace = land-and-expand motion plus supply-side care; pairs your AE muscle with your operator instincts.
  4. RISK: Forge Parts is a Series A startup (small CS team, smaller comp ceiling typically). URL was board-level (forgeparts.com/careers), no specific job page — verify role exists and comp band before applying. Remote posture also unclear."""
,
}


def main():
    ws = ledger._ws()
    rows = ws.get_all_records()
    updates = []
    for idx, r in enumerate(rows, start=2):
        company = r.get("company", "").strip()
        role = r.get("role", "").strip()
 # current notes content — don't overwrite if already populated by JD-mined hypothesis
        current = (r.get("notes") or "").strip()
        if current and current.startswith("FIT HYPOTHESIS"):
            continue
        for (target_company, role_frag), text in MANUAL.items():
            if company == target_company and role_frag.lower() in role.lower():
                updates.append((idx, text))
                print(f"  row {idx}: {company} {role[:38]:38} → manual hypothesis ({len(text)} chars)")
                break

    if updates:
        cells = [{"range": rowcol_to_a1(idx, 8), "values": [[text]]} for idx, text in updates]
        ws.batch_update(cells, value_input_option="RAW")
        print(f"\nwrote {len(updates)} manual hypothesis cells.")
    else:
        print("no manual hypotheses to write.")


if __name__ == "__main__":
    main()
