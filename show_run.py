"""Show what happened in a run: each attempt's answer, the expected answer, and the tool calls.

    python show_run.py                       # latest run, wrong answers only
    python show_run.py --all                 # latest run, every attempt
    python show_run.py --arm A --id D01      # filter by arm and/or question
    python show_run.py runs/<folder>         # a specific run
"""
import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", nargs="?", help="run folder (default: latest)")
    ap.add_argument("--arm")
    ap.add_argument("--id")
    ap.add_argument("--all", action="store_true", help="include correct answers")
    ap.add_argument("--full", action="store_true", help="do not shorten tool output")
    args = ap.parse_args()

    run = Path(args.run) if args.run else max(Path("runs").iterdir(), key=lambda p: p.name)
    rows = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]
    rows.sort(key=lambda r: (r["id"], r["arm"], r["rep"]))
    width = None if args.full else 300
    print(f"Run: {run.name}\n")
    for r in rows:
        if args.arm and r["arm"] != args.arm.upper():
            continue
        if args.id and r["id"] != args.id.upper():
            continue
        if r["correct"] and not args.all:
            continue
        fa = r.get("final_answer") or {}
        print("=" * 90)
        print(f"{r['id']}  Arm {r['arm']}  rep {r['rep']}  ->  {r['outcome'].upper()}")
        print(f"Q: {r['question']}")
        print(f"Expected: {r['expected']}")
        print(f"Answer:   {fa.get('answer_type')}  {fa.get('value')}  {fa.get('unit')}")
        print(f"Why:      {fa.get('explanation')}")
        for s in r["steps"]:
            if s["type"] == "tool":
                print(f"  -> {s['name']}: {json.dumps(s['input'])[:width]}")
                print(f"     {'ERROR ' if s['error'] else ''}{s['output'][:width]}")
        if r.get("error"):
            print(f"  run error: {r['error']}")
    print("=" * 90)


if __name__ == "__main__":
    main()
