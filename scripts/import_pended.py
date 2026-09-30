"""Build data/pended_cases.json from PriorAuth Copilot's decisions on the 22 real MTSamples cases.

Input:  ../priorauth-copilot/results/eval_llm.json  (real notes, LLM evidence, rules decisions)
Output: the 16 pended cases with their pend reason and missing facts.
MTSamples notes are de-identified and carry no provider, so each case is paired, for the demo, with a real NPPES
organisation of the matching specialty (recorded in tests/fixtures), and a demo arrival time so the SLA board shows
every state. Both are marked as demo assignments in the file.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PA = ROOT.parent / "priorauth-copilot"
sys.path.insert(0, str(PA))

from priorauth.rules import evaluate  # noqa: E402

NPI = {"ncd_240_4_cpap": "1720329949", "ncd_100_1_bariatric": "1497941645"}  # real NPPES organisations (NPI-2)
# demo arrival offsets in hours before "now", chosen so the board shows on-track, at-risk and breached journeys
OFFSETS = [2, 20, 60, 100, 130, 150, 160, 170, 5, 30, 66, 80, 110, 140, 1, 175]

if __name__ == "__main__":
    rows = json.loads((PA / "results" / "eval_llm.json").read_text(encoding="utf-8"))["cases"]
    out = []
    for r in rows:
        if r["decision"] != "pend":
            continue
        d = evaluate(r["policy"], {k: v["value"] for k, v in r["facts"].items()})
        i = len(out)
        out.append({"case_id": r["case_id"], "policy_id": r["policy"], "pend_reason": d.pend_reason,
                    "missing_facts": d.missing_facts, "summary": d.summary,
                    "demo": {"ordering_npi": NPI[r["policy"]], "urgency": "expedited" if i % 5 == 0 else "standard",
                             "received_hours_ago": OFFSETS[i % len(OFFSETS)]}})
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "pended_cases.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(len(out), "pended cases;", sum(c["pend_reason"] == "missing_documentation" for c in out), "missing documentation")
