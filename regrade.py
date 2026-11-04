"""Re-grade a saved run with the current grader, without calling the model again.

    python regrade.py runs/<folder>

Keeps the original results.jsonl and summary.txt untouched and writes
results_grader_v<N>.jsonl and summary_grader_v<N>.txt next to them.
"""
import json
import sys
from pathlib import Path

from grader import GRADER_VERSION, grade
from run_experiment import summarize

run = Path(sys.argv[1])
config = json.loads((run / "config.json").read_text())
key = {a["id"]: a for a in json.loads(Path(f"answer_key_{config['question_set']}.json").read_text())["answers"]}
rows = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]

changed = []
for r in rows:
    old = r["outcome"]
    r.update({"outcome": None, "correct": None, "normalized": None})
    r.update(grade(r["final_answer"], key[r["id"]]))
    if r["outcome"] != old:
        changed.append(f"  {r['id']} Arm {r['arm']} rep {r['rep']}: {old} -> {r['outcome']}")

config["grader_version"] = GRADER_VERSION
(run / f"results_grader_v{GRADER_VERSION}.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))
summary = summarize(rows, config, 0)
(run / f"summary_grader_v{GRADER_VERSION}.txt").write_text(summary)
print(f"Re-graded {len(rows)} answers with grader v{GRADER_VERSION}. {len(changed)} changed:")
print("\n".join(changed) or "  (none)")
print("\n" + summary)
