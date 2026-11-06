"""Ask the data: natural-language analytics on the governed semantic layer.

Needs ANTHROPIC_API_KEY in Streamlit secrets. Optional secrets:
  MAX_QUESTIONS_PER_DAY   cap across all visitors (default 200)
  MODEL                   default claude-haiku-5-5
"""
from __future__ import annotations

import re
import threading
from datetime import date

import pandas as pd
import streamlit as st

import analyst
import charts
from semantic_layer import EXT_SNAPSHOT, SemanticLayer

SESSION_CAP = 10
SAMPLES = [   # (button label, full question). Balance-sheet and credit measures that reconcile to FDIC call
              # reports; each is in exec_questions.py. Income ratios (ROA, efficiency) are left out because
              # their governed definitions differ from the quarterly ratios banks publish.
    ("Deposit gainers and losers, New England peers",
     "Show the top 5 deposit gainers and the top 5 decliners among New England banking organizations with "
     "$5B-$50B in assets, by percent change from 2026Q1 to 2026Q2."),
    ("Loan gainers and losers, New England peers",
     "Show the top 5 loan gainers and the top 5 decliners among New England banking organizations with "
     "$5B-$50B in assets, by percent change from 2026Q1 to 2026Q2."),
    ("Loan-to-deposit ratio, New England peers",
     "Loan-to-deposit ratio for New England banking organizations with $5B-$50B in assets, 2026Q2"),
    ("Highest noncurrent loan rates, New England peers",
     "Top 10 New England banking organizations with $5B-$50B in assets by noncurrent loan rate, highest first, 2026Q2"),
    ("Credit quality by New England state",
     "Noncurrent loan rate by New England state, 2026Q2"),
    ("Loan-to-deposit trend, $10B-$50B New England peers",
     "Loan-to-deposit ratio trend by banking organization for New England organizations with $10B-$50B in assets since 2024"),
]


def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:  # no secrets file
        return default


@st.cache_resource
def daily_counter():
    return {"day": None, "count": 0, "lock": threading.Lock()}


def take_daily_slot(cap: int) -> bool:
    c = daily_counter()
    with c["lock"]:
        if c["day"] != date.today():
            c["day"], c["count"] = date.today(), 0
        if c["count"] >= cap:
            return False
        c["count"] += 1
        return True


_LONG_PCT = re.compile(r"(\d+\.\d{3,})(\s?%)")


def md(text: str) -> str:
    """Show percentages at 2 decimals (1.3817% -> 1.38%) and escape $ so Streamlit does not read math."""
    text = _LONG_PCT.sub(lambda m: f"{float(m.group(1)):.2f}{m.group(2)}", str(text))
    return text.replace("$", "\\$")


def label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def draw_chart(chart: dict, result: dict):
    ch, note = charts.build(chart, result)
    st.altair_chart(ch, width="stretch")
    if note:
        st.caption(note)


def render_turn(turn: dict, idx: int, last: bool = False):
    a, results = turn.get("answer"), turn.get("results", {})
    if turn.get("error") and not a:
        st.error("Something went wrong answering that. Please try again, or rephrase the question.")
        return
    if a.get("answer_type") == "decline":
        st.markdown(f"**{md(a.get('headline', 'This data cannot answer that question.'))}**")
        if a.get("decline_reason"):
            st.markdown(md(a["decline_reason"]))
        st.caption("This app covers FDIC call-report data for 2024Q3 to 2026Q2: balance sheets, income, "
                   "profitability and asset-quality ratios, by bank, holding company, state, region, regulator and size.")
        follow_up_buttons(a, idx, last)
        return
    if a.get("correction"):
        st.warning(md(a["correction"]), icon=":material/edit_note:")
    st.markdown(f"#### {md(a.get('headline', ''))}")
    for ins in a.get("insights") or []:
        st.markdown(f"- {md(ins)}")
    chart_specs = analyst.valid_charts(a, results)
    # Scorecard from the result the answer charts; otherwise the latest query (later queries refine earlier
    # ones, e.g. after a name lookup picked the wrong company). Never an earlier, superseded result.
    for rid in [analyst.scorecard_result(chart_specs, results)] if results else []:
        if rid is None:
            break
        r = results[rid]
        table = charts.scorecard(r, r["request"], analyst.LOWER_IS_BETTER, analyst.HIGHER_IS_BETTER)
        if table is not None:
            st.markdown("**Scorecard** (best value highlighted where higher or lower is clearly better"
                        + ("; Unknown, banks with no institution record, left out" if any(
                            str(row.get(d)) == "Unknown" for row in r["rows"] for d in (r["request"].get("dimensions") or []))
                           else "") + ")")
            st.dataframe(table, hide_index=True, width="stretch")
            break
    if chart_specs:
        cols = st.columns(len(chart_specs)) if len(chart_specs) > 1 else [st.container()]
        for col, ch in zip(cols, chart_specs):
            with col:
                draw_chart(ch, results[ch["result_id"]])
    if results:
        with st.expander("How I got this"):
            st.caption("Every number comes from governed metrics in the semantic layer. The AI model chose the "
                       "queries; code wrote and ran the SQL.")
            if turn.get("log"):
                st.markdown("**Steps**")
                st.markdown("\n".join(f"{n}. {md(m)}" for n, m in enumerate(turn["log"], 1)))
            for rid, r in results.items():
                st.markdown(f"**{rid}** · `{', '.join(r['request'].get('metrics', []))}`"
                            + (f" by `{', '.join(r['request']['dimensions'])}`" if r["request"].get("dimensions") else "")
                            + f" · {r['request'].get('period')}")
                if r.get("notes"):
                    st.caption(" ".join(r["notes"]))
                df = pd.DataFrame(r["rows"])
                st.dataframe(df, hide_index=True, width="stretch")
                st.download_button("Download CSV", df.to_csv(index=False), file_name=f"{rid}.csv",
                                   mime="text/csv", key=f"dl_{idx}_{rid}")
                st.code(r.get("sql", ""), language="sql")
    st.caption(f"Semantic model v{next(iter(results.values()))['semantic_model_version'] if results else '?'}"
               f" · {turn.get('latency_s', 0):.0f}s")
    follow_up_buttons(a, idx, last)


def follow_up_buttons(a: dict, idx: int, last: bool):
    ups = (a or {}).get("follow_ups") or []
    if not (last and ups):
        return
    st.markdown("**Ask next:**")
    cols = st.columns(len(ups))
    for j, (col, q) in enumerate(zip(cols, ups)):
        if col.button(q, key=f"fu_{idx}_{j}", width="stretch"):
            st.session_state["pending_q"] = q


# ----------------------------------------------------------------------------- page
st.title("Ask the data")
st.markdown("Ask about FDIC-insured banks, 2024Q3 to 2026Q2: profitability, deposits, loans, asset quality, "
            "by region, size, regulator, state or holding company. Answers come from a governed semantic layer, "
            "so every number follows the official definitions.")

key = secret("ANTHROPIC_API_KEY")
if not key:
    st.info("Asking questions is turned off on this copy of the app. See **The experiment** page for recorded results.")
    st.stop()

history = st.session_state.setdefault("history", [])
used = st.session_state.setdefault("asked", 0)

with st.sidebar:
    if history:      # on the start screen the samples are in the page itself; here they stay reachable
        st.markdown("**Sample questions**")
        for i, (lbl, q) in enumerate(SAMPLES):
            if st.button(lbl, key=f"side_{i}", help=q, width="stretch"):
                st.session_state["pending_q"] = q
    if history and st.button("New conversation", icon=":material/refresh:", width="stretch"):
        st.session_state["history"] = []
        st.rerun()

for i, turn in enumerate(history):
    with st.chat_message("user"):
        st.write(turn["question"])
    with st.chat_message("assistant"):
        render_turn(turn, i, last=(i == len(history) - 1))

question = st.session_state.pop("pending_q", None)
if not history and not question:
    st.markdown("**Try one:**")
    cols = st.columns(3)
    for i, (lbl, q) in enumerate(SAMPLES):
        if cols[i % 3].button(lbl, key=f"sample_{i}", help=q, width="stretch"):
            question = q

typed = st.chat_input("Ask a question about U.S. banks", disabled=used >= SESSION_CAP)
question = typed or question
if question and used >= SESSION_CAP:
    question = None
if used >= SESSION_CAP:
    st.caption(f"You've used all {SESSION_CAP} questions for this session.")
elif history:
    st.caption(f"{SESSION_CAP - used} questions left in this session. Follow-ups use the conversation so far.")

if question:
    with st.chat_message("user"):
        st.write(question)
    with st.chat_message("assistant"):
        if not take_daily_slot(int(secret("MAX_QUESTIONS_PER_DAY", 200))):
            st.warning("The app has reached today's question limit. Please try again tomorrow.")
            st.stop()
        st.session_state["asked"] = used + 1
        layer_key = (id(SemanticLayer), SemanticLayer.__module__)      # rebuilt if the code was reloaded
        if st.session_state.get("layer_key") != layer_key:
            st.session_state["layer"], st.session_state["layer_key"] = SemanticLayer(ext=EXT_SNAPSHOT, pro_forma=True), layer_key
        import anthropic
        client = anthropic.Anthropic(api_key=key, max_retries=4)
        with st.status("Working on it…", expanded=False) as status:
            try:
                turn = analyst.run(client, secret("MODEL", "claude-haiku-5-5"), st.session_state["layer"], question,
                                   history, on_step=lambda m: status.write(m))
                status.update(label="Done", state="complete")
            except Exception:  # noqa: BLE001 - never show a traceback to visitors
                import logging
                logging.exception("Ask page failed on question: %s", question)
                turn = {"answer": None, "results": {}, "error": "unexpected"}
                status.update(label="Something went wrong", state="error")
        turn["question"] = question
        history.append(turn)
        st.rerun()   # redraw from history: clean layout, samples hidden, counter current
