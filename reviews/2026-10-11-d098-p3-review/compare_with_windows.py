"""Linux P3 matrix vs the Windows evidence: every non-timing field of each scenario must match."""
import json, sys
linux, windows = (json.load(open(p)) for p in sys.argv[1:3])
keys = ("status", "reason", "damage_points", "physics_steps", "applied_input_digest", "invalidations", "reproofs",
        "recovery_proof_obtained", "decisions")
w = {}
for r in windows["rows"]:
    w.setdefault(r["scenario"], {tuple(r[k] if not isinstance(r[k], list) else tuple(r[k]) for k in keys)})
diff = [r["scenario"] for r in linux["rows"]
        if tuple(r[k] if not isinstance(r[k], list) else tuple(r[k]) for k in keys) not in w[r["scenario"]]]
print("Linux scenarios:", len(linux["rows"]), "| Windows rounds:", windows["rounds"], "| non-timing mismatches:", diff)
print("Linux latency:", linux["summary"]["decision_latency_ms"])
print("Windows latency:", windows["summary"]["decision_latency_ms"])
