"""Run the experiment: questions x arms x repetitions. Writes a run folder with
the full configuration, every answer and its grade, and a summary.

    export ANTHROPIC_API_KEY=...            # loaded with read -s, never typed into a file
    python run_experiment.py --questions D01,D08,D29 --reps 1 --label smoke
    python run_experiment.py --reps 3 --label dev-baseline

Output: runs/<timestamp>_<label>/config.json, results.jsonl, summary.txt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anthropic
import yaml

import prompts
from agent import RawDatabase, Tools, run_question
from grader import GRADER_VERSION, grade
from semantic_layer import SemanticLayer

ROOT = Path(__file__).parent
_local = threading.local()


def tools_for(arm):
    """One database connection per thread and arm (DuckDB connections are not shared across threads)."""
    cache = getattr(_local, "tools", None)
    if cache is None:
        cache = _local.tools = {}
    if arm not in cache:
        cache[arm] = Tools(arm, semantic=SemanticLayer()) if arm == "C" else Tools(arm, raw_db=RawDatabase())
    return cache[arm]


def git_commit():
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
        dirty = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT).returncode != 0
        return sha + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def check_heldout_lock(path: Path):
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    decisions = (ROOT / "DECISIONS.md").read_text()
    if sha not in decisions:
        sys.exit(f"Held-out file fingerprint {sha[:12]}… is not the one recorded in DECISIONS.md. Refusing to run.")
    return sha


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="dev", choices=["dev", "heldout"])
    ap.add_argument("--final", action="store_true", help="required to run the held-out set")
    ap.add_argument("--arms", default="A,B,C")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--questions", default="", help="comma-separated ids, default all")
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--temperature", type=float, default=None, help="default: the API default")
    ap.add_argument("--max-turns", type=int, default=12)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Run: read -s ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY")
    qfile = ROOT / f"questions_{args.set}.yaml"
    if args.set == "heldout":
        if not args.final:
            sys.exit("The held-out set is locked. Use --final only for the final run.")
        check_heldout_lock(qfile)

    questions = yaml.safe_load(qfile.read_text())["questions"]
    key = {a["id"]: a for a in json.loads((ROOT / f"answer_key_{args.set}.json").read_text())["answers"]}
    if args.questions:
        wanted = [q.strip() for q in args.questions.split(",")]
        questions = [q for q in questions if q["id"] in wanted]
    arms = [a.strip().upper() for a in args.arms.split(",")]
    specs = {arm: prompts.build(arm) for arm in arms}

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{args.label}"
    out_dir = ROOT / "runs" / run_id
    out_dir.mkdir(parents=True)
    config = {
        "run_id": run_id, "started": datetime.now().isoformat(timespec="seconds"),
        "model": args.model, "temperature": args.temperature, "max_turns": args.max_turns,
        "question_set": args.set, "questions_sha256": hashlib.sha256(qfile.read_bytes()).hexdigest(),
        "question_ids": [q["id"] for q in questions], "arms": arms, "reps": args.reps,
        "prompt_version": prompts.PROMPT_VERSION, "grader_version": GRADER_VERSION, "semantic_model_version": prompts.MODEL["version"],
        "prompt_sha256": {a: s["prompt_sha256"] for a, s in specs.items()},
        "git_commit": git_commit(), "anthropic_sdk": anthropic.__version__,
    }
    (out_dir / "config.json").write_text(json.dumps(config, indent=2))

    client = anthropic.Anthropic(max_retries=6)
    jobs = [(arm, rep, q) for rep in range(1, args.reps + 1) for arm in arms for q in questions]
    print(f"Run {run_id}: {len(questions)} questions x {len(arms)} arms x {args.reps} reps = {len(jobs)} attempts")

    def work(job):
        arm, rep, q = job
        rec = run_question(client, args.model, specs[arm], tools_for(arm), q["question"],
                           max_turns=args.max_turns, temperature=args.temperature)
        g = grade(rec["final_answer"], key[q["id"]])
        return {"arm": arm, "rep": rep, "id": q["id"], "category": q["category"], "question": q["question"],
                "expected": key[q["id"]]["expected"], **g, **rec}

    results, lock, t0 = [], threading.Lock(), time.time()
    with open(out_dir / "results.jsonl", "w") as fh, ThreadPoolExecutor(args.workers) as pool:
        for i, fut in enumerate(as_completed([pool.submit(work, j) for j in jobs]), 1):
            r = fut.result()
            with lock:
                results.append(r)
                fh.write(json.dumps(r, default=str) + "\n")
                fh.flush()
            mark = "✓" if r["correct"] else "✗"
            print(f"  [{i:>3}/{len(jobs)}] {r['arm']} rep{r['rep']} {r['id']} {mark} {r['outcome']}"
                  + (f"  ({r['error']})" if r["error"] else ""))

    summary = summarize(results, config, time.time() - t0)
    (out_dir / "summary.txt").write_text(summary)
    print("\n" + summary)
    print(f"Saved to runs/{run_id}/")


def summarize(results, config, elapsed):
    L = [f"Run {config['run_id']}  model={config['model']}  prompts v{config['prompt_version']}  "
         f"semantic model v{config['semantic_model_version']}  grader v{config.get('grader_version', 1)}"
         + (f"  ({elapsed / 60:.1f} min)" if elapsed else ""), ""]
    arms = config["arms"]
    by = defaultdict(list)
    for r in results:
        by[r["arm"]].append(r)

    L.append(f"{'':16}" + "".join(f"{'Arm ' + a:>12}" for a in arms))
    def row(label, fn):
        L.append(f"{label:16}" + "".join(f"{fn(by[a]):>12}" for a in arms))
    pct = lambda rs, f=lambda r: r["correct"]: f"{100 * sum(map(f, rs)) / len(rs):.0f}%" if rs else "-"
    row("Accuracy", pct)
    cats = sorted({r["category"] for r in results}, key=["ytd", "units", "ratio", "growth", "population", "ontology", "out_of_scope"].index)
    for c in cats:
        row(f"  {c}", lambda rs, c=c: pct([r for r in rs if r["category"] == c]))

    reps = config["reps"]
    if reps > 1:
        def consistent(rs):
            g = defaultdict(set)
            for r in rs:
                g[r["id"]].add(r["correct"])
            return f"{100 * sum(len(v) == 1 for v in g.values()) / len(g):.0f}%" if g else "-"
        row(f"Same result x{reps}", consistent)
    row("Avg tool calls", lambda rs: f"{sum(r['tool_calls'] for r in rs) / len(rs):.1f}" if rs else "-")
    row("Avg tokens in", lambda rs: f"{sum(r['usage']['input'] + r['usage']['cache_read'] + r['usage']['cache_write'] for r in rs) / len(rs):,.0f}" if rs else "-")
    row("Avg tokens out", lambda rs: f"{sum(r['usage']['output'] for r in rs) / len(rs):,.0f}" if rs else "-")
    row("Avg seconds", lambda rs: f"{sum(r['latency_s'] for r in rs) / len(rs):.1f}" if rs else "-")

    L.append("")
    L.append("Outcomes")
    outcomes = ["correct", "wrong_value", "wrong_units", "wrong_type", "missing_value", "false_decline", "missed_decline", "no_answer"]
    for o in outcomes:
        if any(r["outcome"] == o for r in results):
            row(f"  {o}", lambda rs, o=o: str(sum(r["outcome"] == o for r in rs)))

    L.append("")
    L.append("Per question (correct / attempts)")
    ids = sorted({r["id"] for r in results})
    for qid in ids:
        cells = []
        for a in arms:
            rs = [r for r in by[a] if r["id"] == qid]
            cells.append(f"{sum(r['correct'] for r in rs)}/{len(rs)}")
        L.append(f"  {qid:14}" + "".join(f"{c:>12}" for c in cells))
    return "\n".join(L)


if __name__ == "__main__":
    main()
