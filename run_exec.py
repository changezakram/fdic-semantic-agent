"""Run the executive questions through the app's analyst and grade every answer in code.

    python run_exec.py                     # all questions, 3 runs each
    python run_exec.py --reps 1 --questions E02,E03
    python run_exec.py --label before-push

Uses ANTHROPIC_API_KEY from the environment, or from .streamlit/secrets.toml (git-ignored).
Writes runs/exec_<timestamp>_<label>/results.jsonl and summary.txt. No AI judge.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anthropic

import analyst
import exec_questions
from semantic_layer import EXT_SNAPSHOT, SemanticLayer

ROOT = Path(__file__).parent
_local = threading.local()
_NUM = re.compile(r"\d[\d,]*\.?\d*")


def secret(name, default=None):
    if os.environ.get(name):
        return os.environ[name]
    p = ROOT / ".streamlit" / "secrets.toml"
    if p.exists():
        return tomllib.loads(p.read_text()).get(name, default)
    return default


def layer():
    if getattr(_local, "layer", None) is None:
        _local.layer = SemanticLayer(ext=EXT_SNAPSHOT, pro_forma=True)
    return _local.layer


def numbers_in(text: str) -> list[tuple[float, int]]:
    out = []
    for m in _NUM.finditer(text):
        raw = m.group().replace(",", "").rstrip(".")
        try:
            out.append((float(raw), len(raw.split(".")[1]) if "." in raw else 0))
        except ValueError:
            pass
    return out


def grade(q: dict, turn: dict) -> list[str]:
    """Return the list of failures (empty = pass)."""
    a = turn.get("answer")
    if not a:
        return [f"no answer ({turn.get('error')})"]
    kind = a.get("answer_type")
    if q["expect"] == "decline":
        return [] if kind == "decline" else ["should have declined but answered"]
    if kind != "answer":
        return ["declined a question it should answer"]
    fails = []
    head = a.get("headline", "")
    body = " ".join([head] + list(a.get("insights") or []))
    for group in q.get("headline", []):
        if not any(alt.lower() in head.lower() for alt in group):
            fails.append(f"headline missing {group}")
    for group in q.get("must_text", []):
        if not any(alt.lower() in body.lower() for alt in group):
            fails.append(f"answer missing {group}")
    for s in q.get("not_headline", []):
        if s.lower() in head.lower():
            fails.append(f"headline mentions {s}")
    for s in q.get("not_text", []):
        if s.lower() in body.lower():
            fails.append(f"answer mentions {s}")
    found = numbers_in(body)
    for v in q.get("numbers", []):
        if not any(d >= 1 and abs(abs(v) - n) <= 0.5 * 10 ** -d + 1e-9 for n, d in found):   # -1.765 may read 1.76 or 1.77
            fails.append(f"number {abs(v):.2f} not stated")
    problems = (analyst.check_claims(a, turn["results"]) + analyst.check_percentages(a, turn["results"], turn.get("calc", []))
                + analyst.check_ranges(a, turn["results"]))
    fails += [f"checker: {p[:120]}" for p in problems]
    if q.get("chart") and not analyst.valid_charts(a, turn["results"]):
        fails.append("no valid chart")
    fails += [f"display: {p}" for p in analyst.display_problems(a, turn["results"])]
    return fails


def run_one(client, model, q, rep):
    turn = analyst.run(client, model, layer(), q["question"])
    fails = grade(q, turn)
    a = turn.get("answer") or {}
    return {"id": q["id"], "rep": rep, "topic": q["topic"], "question": q["question"], "pass": not fails,
            "fails": fails, "headline": a.get("headline"), "insights": a.get("insights"),
            "answer_type": a.get("answer_type"), "steps": turn.get("steps"), "usage": turn.get("usage"),
            "latency_s": turn.get("latency_s"), "error": turn.get("error")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--questions", default="")
    ap.add_argument("--label", default="exec")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    key = secret("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("No API key: set ANTHROPIC_API_KEY or add it to .streamlit/secrets.toml")
    model = secret("MODEL", "claude-haiku-5-5")
    client = anthropic.Anthropic(api_key=key, max_retries=4)

    qs = exec_questions.build()
    if args.questions:
        want = set(args.questions.split(","))
        qs = [q for q in qs if q["id"] in want]
    out = ROOT / "runs" / f"exec_{datetime.now():%Y%m%d_%H%M%S}_{args.label}"
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(q, r) for q in qs for r in range(1, args.reps + 1)]
    print(f"{len(jobs)} runs ({len(qs)} questions x {args.reps}) on {model}, semantic model v{layer().version}")

    results = []
    with ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(run_one, client, model, q, r): (q, r) for q, r in jobs}
        for f in as_completed(futs):
            q, r = futs[f]
            try:
                res = f.result()
            except Exception as e:  # noqa: BLE001
                res = {"id": q["id"], "rep": r, "topic": q["topic"], "question": q["question"], "pass": False,
                       "fails": [f"crash: {type(e).__name__}: {e}"]}
            results.append(res)
            print(f"  {res['id']} run {r}: {'PASS' if res['pass'] else 'FAIL  ' + '; '.join(res['fails'])[:150]}")

    results.sort(key=lambda x: (x["id"], x["rep"]))
    with open(out / "results.jsonl", "w") as fh:
        for r in results:
            fh.write(json.dumps(r, default=str) + "\n")

    lines = [f"Executive questions, {datetime.now():%Y-%m-%d %H:%M}, model {model}, semantic model v{layer().version}", ""]
    for q in qs:
        rs = [r for r in results if r["id"] == q["id"]]
        ok = sum(r["pass"] for r in rs)
        lines.append(f"{q['id']}  {ok}/{len(rs)}  {q['topic']:<32} {q['question'][:70]}")
        for r in rs:
            if not r["pass"]:
                lines.append(f"      run {r['rep']}: {'; '.join(r['fails'])[:200]}")
    total = sum(r["pass"] for r in results)
    lines += ["", f"TOTAL  {total}/{len(results)} runs fully correct ({total / max(len(results), 1):.0%})",
              f"Questions correct in every run: {sum(all(r['pass'] for r in results if r['id'] == q['id']) for q in qs)}/{len(qs)}"]
    (out / "summary.txt").write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
