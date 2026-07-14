# Primitives library — schema

<!-- clean-docs:purpose -->
Atomic, grounded, tagged units of the operator persona's profile. The prep agent SELECTS a role-specific subset and the composition layer (positioning-spine = composition rules) weaves them into coherent, non-recycled materials. Replaces prose masters as the *basis material*; the spine governs *how* selected primitives are composed.
<!-- clean-docs:end purpose -->


Note: the operator persona ("Jordan Avery") is a fictional engineer used to
demonstrate the retrieval engine. Not anyone's real history.

One JSON object per line in `primitives.jsonl`:

```
{
 "id":        "acc-revenue-at-risk",          # stable slug
 "type":      "accomplishment|capability|skill|experience|proof_point|domain",
 "claim":     "<canonical phrasing>",          # the renderable text
 "variants":  {"short":"<~8 words>", "long":"<fuller>"},
 "tags": {
   "archetypes": ["intersection","bridge","commercial","leadership"],   # role_archetype vocab
   "domains":    ["industrial","manufacturing","cad","additive","ai","b2b","saas","data"],
   "themes":     ["build","ml","agentic","revenue","customer","domain","sales-method"]
 },
 "proof":      "<grounded source: file / repo / memory ref>",
 "provenance": "CONFIRMED|PROVEN-CODE|SUGGESTED",   # never SUGGESTED for a hard claim
 "strength":   1-5
}
```

RULES: every primitive is grounded in the synthetic persona (no invention beyond
the fictional persona). `provenance` gates use — hard claims require CONFIRMED or
PROVEN-CODE. The industrial operating-role employer is referred to generically
("a heavy-duty industrial parts manufacturer"), not by a specific named account.
The verify gate checks composed claims trace to a primitive.
