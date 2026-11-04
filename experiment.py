"""The experiment page: the same AI model, the same FDIC data, three ways of giving it business context.

    Shown as a page of app.py (streamlit run app.py).

Recorded mode (default) shows the frozen final runs from runs/*_final-*/ — the same answers
the article's numbers come from. Live mode runs a chosen experiment question through setup C
and grades it; it is on only when the app has ANTHROPIC_API_KEY in its secrets. Limits: 5 live
runs per visitor session, MAX_LIVE_PER_DAY (default 200) across all visitors.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import streamlit as st
import yaml

ROOT = Path(__file__).parent
REPO_URL = "https://github.com/changezakram/fdic-semantic-agent"

ARMS = {
    "A": ("No semantic model", "Table and column names only. Writes its own SQL."),
    "B": ("Semantic model as documentation", "The governed definitions in its prompt. Writes its own SQL."),
    "C": ("Semantic model enforced", "The same definitions, applied in code. No SQL access."),
}
SETS = {
    "final-dev": ("Single-trap questions", "dev"),
    "final-heldout": ("Single-trap questions, held out", "heldout"),
    "final-hard-dev": ("Multi-rule questions", "hard_dev"),
    "final-hard-heldout": ("Multi-rule questions, held out", "hard_heldout"),
}
OUTCOME_LABEL = {
    "correct": ("Correct", "green"),
    "wrong_value": ("Wrong answer", "red"),
    "wrong_units": ("Wrong units", "red"),
    "wrong_type": ("Wrong kind of answer", "red"),
    "missing_value": ("No value given", "red"),
    "false_decline": ("Declined (answerable)", "orange"),
    "missed_decline": ("Answered (should decline)", "red"),
    "no_answer": ("No answer", "gray"),
}
CATEGORY_LABEL = {
    "ytd": "Year-to-date income", "units": "Units ($ thousands)", "ratio": "Industry ratios",
    "growth": "Growth across quarters", "population": "Who counts as a bank", "ontology": "Holding companies and regions",
    "multi_rule": "Several rules combined", "beyond_layer": "Outside the semantic layer's design",
    "out_of_scope": "Data can't answer",
}



# ----------------------------------------------------------------------------- data
@st.cache_data
def load_runs():
    """Latest final run for each question set: {set_label: {"config", "rows", "questions"}}."""
    runs = {}
    for label, (_, qset) in SETS.items():
        folders = sorted(p for p in (ROOT / "runs").glob(f"*_{label}") if p.is_dir())
        if not folders:
            continue
        folder = folders[-1]
        rows = [json.loads(l) for l in (folder / "results.jsonl").read_text().splitlines() if l.strip()]
        questions = {q["id"]: q for q in yaml.safe_load((ROOT / f"questions_{qset}.yaml").read_text())["questions"]}
        runs[label] = {"config": json.loads((folder / "config.json").read_text()), "rows": rows,
                       "questions": questions, "folder": folder.name}
    return runs


def fmt_value(answer: dict | None) -> str:
    if not answer:
        return "—"
    if answer.get("answer_type") == "decline":
        return "Declined"
    v, unit = answer.get("value"), answer.get("unit", "none")
    if isinstance(v, list):
        return "\n".join(f"{i}. {x}" for i, x in enumerate(v, 1))
    if isinstance(v, (int, float)):
        if unit == "usd_billions":
            return f"${v:,.2f}B" if abs(v) < 10 else f"${v:,.1f}B"
        if unit == "usd_trillions":
            return f"${v:,.2f}T"
        if unit == "usd_millions":
            return f"${v:,.1f}M"
        if unit == "usd_thousands":
            return f"{v:,.0f} (in $ thousands)"
        if unit == "usd":
            return f"${v:,.0f}"
        if unit == "percent":
            return f"{v:,.2f}%"
        if unit == "percentage_points":
            return f"{v:+.2f} pts"
        if unit == "count":
            return f"{v:,.0f}"
        return f"{v:,}"
    return str(v)


def fmt_expected(qdef: dict, expected) -> str:
    t = qdef["type"]
    if t == "decline":
        return "Decline: " + qdef.get("reason", "the data cannot answer this.")
    if expected is None:
        return "—"
    return {
        "dollars_bn": lambda e: f"${e:,.1f}B" if abs(e) >= 10 else f"${e:,.3f}B",
        "dollars_tn": lambda e: f"${e:,.2f}T",
        "ratio_pct": lambda e: f"{e:.2f}%",
        "growth_pct": lambda e: f"{e:.2f}%",
        "pp_change": lambda e: f"{e:+.2f} pts",
        "count": lambda e: f"{e:,}",
        "label": str,
        "list": lambda e: ", ".join(e),
    }[t](expected)


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "—"


# ----------------------------------------------------------------------------- header
runs = load_runs()
st.title("The experiment: where accuracy comes from")
st.markdown(
    "One AI model (Claude Haiku 5.5), the same public FDIC call-report data, the same questions. "
    "Only one thing changes: how much governed business context the model gets, and how it is delivered."
)

if not runs:
    st.error("No final runs found. Expected folders like runs/<timestamp>_final-dev/ next to this app.")
    st.stop()

all_rows = [r for run in runs.values() for r in run["rows"]]
in_design = [r for r in all_rows if r["category"] != "beyond_layer"]
cols = st.columns(3)
for col, (arm, (name, desc)) in zip(cols, ARMS.items()):
    rs = [r for r in in_design if r["arm"] == arm]
    col.metric(f"{arm}. {name}", pct(sum(r["correct"] for r in rs), len(rs)), help=desc)
    col.caption(f"Correct on {len(rs)} in-scope attempts")

tab_compare, tab_results, tab_live, tab_about = st.tabs(["Compare answers", "Results", "Run it live", "About"])

# ----------------------------------------------------------------------------- compare
with tab_compare:
    c1, c2, c3 = st.columns([2, 4, 1])
    set_names = {SETS[k][0]: k for k in runs}
    set_label = set_names[c1.selectbox("Question set", list(set_names))]
    run = runs[set_label]
    rows = run["rows"]
    by_q = defaultdict(list)
    for r in rows:
        by_q[r["id"]].append(r)

    only_disagree = c1.checkbox("Only questions where the setups disagree", value=True)
    ids = sorted(by_q)
    if only_disagree:
        ids = [i for i in ids if len({r["correct"] for r in by_q[i]}) > 1] or sorted(by_q)

    q_names = {f"{i}  ·  {run['questions'][i]['question']}": i for i in ids}
    qid = q_names[c2.selectbox("Question", list(q_names), key=f"q_{set_label}_{only_disagree}")]
    reps = sorted({r["rep"] for r in by_q[qid]})
    rep_names = {f"Run {n} of {len(reps)}": n for n in reps}
    differs = [i for i, n in enumerate(reps) if len({x["correct"] for x in by_q[qid] if x["rep"] == n}) > 1]
    rep = rep_names[c3.selectbox("Run", list(rep_names), index=differs[0] if differs else 0, key=f"rep_{set_label}_{qid}")]

    qdef = run["questions"][qid]
    first = by_q[qid][0]
    st.markdown(f"#### {qdef['question']}")
    meta = f"**Correct answer:** {fmt_expected(qdef, first['expected'])}"
    meta += f"  ·  **What it tests:** {CATEGORY_LABEL.get(qdef['category'], qdef['category'])}"
    st.markdown(meta)
    if qdef.get("trap"):
        st.caption(f"The trap: {qdef['trap']}")

    cols = st.columns(3)
    for col, arm in zip(cols, ARMS):
        r = next((x for x in by_q[qid] if x["arm"] == arm and x["rep"] == rep), None)
        with col:
            with st.container(border=True):
                st.markdown(f"**{arm}. {ARMS[arm][0]}**")
                if r is None:
                    st.write("No attempt recorded.")
                    continue
                label, color = OUTCOME_LABEL.get(r["outcome"], (r["outcome"], "gray"))
                st.markdown(f":{color}-background[{label}]")
                st.markdown(f"### {fmt_value(r.get('final_answer'))}")
                why = (r.get("final_answer") or {}).get("explanation")
                if why:
                    st.write(why)
                tally = [x["correct"] for x in by_q[qid] if x["arm"] == arm]
                st.caption(f"Correct in {sum(tally)} of {len(tally)} runs · {r['tool_calls']} tool calls · "
                           f"{r['latency_s']:.1f}s")
                steps = [s for s in r["steps"] if s["type"] == "tool"]
                with st.expander(f"What it ran ({len(steps)})"):
                    for i, s in enumerate(steps, 1):
                        st.markdown(f"**{i}. {s['name']}**" + ("  :red[error]" if s.get("error") else ""))
                        if s["name"] == "run_sql":
                            st.code(s["input"].get("sql", ""), language="sql")
                        else:
                            st.code(json.dumps(s["input"], indent=2), language="json")
                        st.caption(s["output"][:600] + ("…" if len(s["output"]) > 600 else ""))

# ----------------------------------------------------------------------------- results
with tab_results:
    st.markdown("Accuracy by question set. Every attempt is graded by code against a hand-checked answer key.")
    table = []
    for label, run in runs.items():
        row = {"Question set": SETS[label][0], "Attempts per setup": sum(1 for r in run["rows"] if r["arm"] == "A")}
        for arm in ARMS:
            rs = [r for r in run["rows"] if r["arm"] == arm]
            row[f"{arm}. {ARMS[arm][0]}"] = pct(sum(r["correct"] for r in rs), len(rs))
        table.append(row)
    st.dataframe(table, hide_index=True, width="stretch")

    st.markdown("**Same result in every run**, by question set (share of questions where all runs agreed)")
    cons = []
    for label, run in runs.items():
        row = {"Question set": SETS[label][0]}
        for arm in ARMS:
            g = defaultdict(set)
            for r in run["rows"]:
                if r["arm"] == arm:
                    g[r["id"]].add(r["correct"])
            row[f"{arm}. {ARMS[arm][0]}"] = pct(sum(len(v) == 1 for v in g.values()), len(g))
        cons.append(row)
    st.dataframe(cons, hide_index=True, width="stretch")

    st.markdown("**How each setup missed**, across all four sets")
    outcomes = {}
    for o, (lab, _) in OUTCOME_LABEL.items():
        if o == "correct":
            continue
        counts = {f"{a}. {ARMS[a][0]}": sum(1 for r in all_rows if r["arm"] == a and r["outcome"] == o) for a in ARMS}
        if any(counts.values()):
            outcomes[lab] = counts
    st.dataframe([{"Outcome": k, **v} for k, v in outcomes.items()], hide_index=True, width="stretch")
    cfg = next(iter(runs.values()))["config"]
    st.caption(f"Model {cfg['model']} · prompts v{cfg['prompt_version']} · semantic model v{cfg['semantic_model_version']}"
               f" · grader v{cfg.get('grader_version', '?')} · code {cfg.get('git_commit', '?')}")

# ----------------------------------------------------------------------------- live
@st.cache_resource
def live_counter():
    """Shared across all visitors of this app instance: live runs per day."""
    import threading
    return {"day": None, "count": 0, "lock": threading.Lock()}


@st.cache_data
def all_questions():
    """Every question with its answer key, grouped by set: {display label: (set_label, question, key)}."""
    out = {}
    for label, (set_name, qset) in SETS.items():
        qs = yaml.safe_load((ROOT / f"questions_{qset}.yaml").read_text())["questions"]
        keys = {a["id"]: a for a in json.loads((ROOT / f"answer_key_{qset}.json").read_text())["answers"]}
        for q in qs:
            out[f"{set_name}  ·  {q['id']}  ·  {q['question']}"] = (label, q, keys[q["id"]])
    return out


with tab_live:
    try:
        key = st.secrets.get("ANTHROPIC_API_KEY")
        daily_cap = int(st.secrets.get("MAX_LIVE_PER_DAY", 200))
    except Exception:  # no secrets file: live mode stays off
        key, daily_cap = None, 0
    session_cap = 5

    st.markdown(f"Pick a question and run it now through **C. {ARMS['C'][0]}**: the AI model can only call governed "
                "metrics, and code writes the SQL. The answer is graded against the same hand-checked key as the experiment.")
    if not key:
        st.info("Live runs are turned off on this copy of the demo. The recorded answers in the first tab "
                "are the ones the results come from.")
    else:
        catalog = all_questions()
        choice = st.selectbox("Question", list(catalog), key="live_choice")
        used = st.session_state.get("live_used", 0)
        left = st.empty()
        if st.button("Run it live", type="primary", disabled=used >= session_cap):
            from datetime import date
            counter = live_counter()
            with counter["lock"]:
                if counter["day"] != date.today():
                    counter["day"], counter["count"] = date.today(), 0
                allowed = counter["count"] < daily_cap
                if allowed:
                    counter["count"] += 1
            if not allowed:
                st.warning("The demo has reached today's limit for live runs. Try again tomorrow, or browse the "
                           "recorded answers in the first tab.")
            else:
                st.session_state["live_used"] = used + 1
                import anthropic
                import prompts
                from agent import Tools, run_question
                from grader import grade
                from semantic_layer import SemanticLayer

                set_label, qdef, akey = catalog[choice]
                model = next(iter(runs.values()))["config"]["model"]
                client = anthropic.Anthropic(api_key=key, max_retries=4)
                with st.spinner("Running through the semantic layer…"):
                    r = run_question(client, model, prompts.build("C"), Tools("C", semantic=SemanticLayer()), qdef["question"])
                g = grade(r["final_answer"], akey)
                label, color = OUTCOME_LABEL.get(g["outcome"], (g["outcome"], "gray"))
                with st.container(border=True):
                    st.markdown(f"**C. {ARMS['C'][0]}** · :{color}-background[{label}]")
                    st.markdown(f"### {fmt_value(r['final_answer'])}")
                    st.markdown(f"**Correct answer:** {fmt_expected(qdef, akey['expected'])}")
                    st.write((r["final_answer"] or {}).get("explanation") or r.get("error") or "")
                    st.caption(f"{r['tool_calls']} tool calls · {r['latency_s']:.1f}s")
                    steps = [s for s in r["steps"] if s["type"] == "tool"]
                    with st.expander(f"What it ran ({len(steps)})"):
                        for i, s in enumerate(steps, 1):
                            st.markdown(f"**{i}. {s['name']}**" + ("  :red[error]" if s.get("error") else ""))
                            st.code(json.dumps(s["input"], indent=2), language="json")
                            st.caption(s["output"][:600] + ("…" if len(s["output"]) > 600 else ""))
                recorded = [x for x in runs.get(set_label, {}).get("rows", []) if x["id"] == qdef["id"]]
                if recorded:
                    st.caption("In the recorded final run, this question scored: " + " · ".join(
                        f"{a}. {sum(x['correct'] for x in recorded if x['arm'] == a)}/"
                        f"{sum(1 for x in recorded if x['arm'] == a)}" for a in ARMS))
        left.caption(f"{session_cap - st.session_state.get('live_used', 0)} of {session_cap} live runs left in this session.")

# ----------------------------------------------------------------------------- about
with tab_about:
    st.markdown(f"""
**The question.** Does accuracy in AI analytics come from the model, or from governed business context?

**The setups.**
- **A. No semantic model:** the raw table and column names; the model writes its own SQL.
- **B. Semantic model as documentation:** the same, plus a governed glossary rendered from the semantic model.
- **C. Semantic model enforced:** the same glossary, but the model can only call governed metrics; code writes the SQL.

**The data.** Eight quarters of public FDIC call reports (2024Q3–2026Q2), about 4,300–4,600 banks per quarter,
frozen in one snapshot.

**The method.** 58 questions with hand-checked answers, graded by code with no AI judge. Held-out questions were locked
before any tuning. Each question ran three times per setup on a frozen configuration.

**Everything is public:** code, data, prompts, every transcript and every decision: [{REPO_URL}]({REPO_URL})
""")
