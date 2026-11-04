"""Ask the data: natural-language analytics on the governed semantic layer.

Needs ANTHROPIC_API_KEY in Streamlit secrets. Optional secrets:
  MAX_QUESTIONS_PER_DAY   cap across all visitors (default 200)
  MODEL                   default claude-haiku-5-5
"""
from __future__ import annotations

import threading
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st

import analyst
from semantic_layer import SemanticLayer

SESSION_CAP = 10
SAMPLES = [
    "How has industry ROA trended since 2024?",
    "Net income by FDIC region in 2026Q2",
    "Noncurrent loan rate by size class, last year",
    "Deposit growth by regulator, year over year",
    "Top 10 banking organizations by assets",
    "Community banks vs. others: net income trend",
]
UNIT_SUFFIX = {"$ billions": "($B)", "percent": "(%)", "count": ""}


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


def md(text: str) -> str:
    """Escape $ so Streamlit does not read dollar amounts as math."""
    return str(text).replace("$", "\\$")


def label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def draw_chart(chart: dict, result: dict):
    df = pd.DataFrame(result["rows"])
    x, y, color = chart["x"], chart["y"], chart.get("color")
    unit = result.get("units", {}).get(y, "")
    y_title = f"{label(y)} {UNIT_SUFFIX.get(unit, '')}".strip()
    tooltip = [alt.Tooltip(x, title=label(x)), alt.Tooltip(y, title=y_title, format=",.2f")]
    if color:
        tooltip.insert(1, alt.Tooltip(color, title=label(color)))
    if chart["kind"] == "line":
        enc = dict(x=alt.X(f"{x}:O", title=label(x), sort=None, axis=alt.Axis(labelAngle=0)), y=alt.Y(f"{y}:Q", title=y_title), tooltip=tooltip)
        if color:
            enc["color"] = alt.Color(f"{color}:N", title=label(color))
        base = alt.Chart(df).encode(**enc)
        ch = base.mark_line(strokeWidth=2.5) + base.mark_circle(size=60)
    else:
        enc = dict(y=alt.Y(f"{x}:N", title=None, sort="-x"), x=alt.X(f"{y}:Q", title=y_title), tooltip=tooltip)
        if color:
            enc["color"] = alt.Color(f"{color}:N", title=label(color))
            enc["yOffset"] = alt.YOffset(f"{color}:N")
        ch = alt.Chart(df).mark_bar(cornerRadiusEnd=3).encode(**enc)
    st.altair_chart(ch.properties(title=chart.get("title", ""), height=320), width="stretch")


def render_turn(turn: dict, idx: int, last: bool = False):
    a, results = turn.get("answer"), turn.get("results", {})
    if turn.get("error") and not a:
        st.error(f"Something went wrong: {turn['error']}. Try rephrasing the question.")
        return
    if a.get("answer_type") == "decline":
        st.markdown(f"**{md(a.get('headline', 'This data cannot answer that question.'))}**")
        if a.get("decline_reason"):
            st.markdown(md(a["decline_reason"]))
        st.caption("This app covers FDIC call-report data for 2024Q3 to 2026Q2: balance sheets, income, "
                   "profitability and asset-quality ratios, by bank, holding company, state, region, regulator and size.")
        follow_up_buttons(a, idx, last)
        return
    st.markdown(f"#### {md(a.get('headline', ''))}")
    for ins in a.get("insights") or []:
        st.markdown(f"- {md(ins)}")
    charts = analyst.valid_charts(a, results)
    if charts:
        cols = st.columns(len(charts)) if len(charts) > 1 else [st.container()]
        for col, ch in zip(cols, charts):
            with col:
                draw_chart(ch, results[ch["result_id"]])
    if results:
        with st.expander("How I got this"):
            st.caption("Every number comes from governed metrics in the semantic layer. The AI model chose the "
                       "queries; code wrote and ran the SQL.")
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
    st.markdown("**Sample questions**")
    for i, s in enumerate(SAMPLES):
        if st.button(s, key=f"side_{i}", width="stretch"):
            st.session_state["pending_q"] = s
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
    for i, s in enumerate(SAMPLES):
        if cols[i % 3].button(s, key=f"sample_{i}", width="stretch"):
            question = s

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
        if "layer" not in st.session_state:
            st.session_state["layer"] = SemanticLayer()
        import anthropic
        client = anthropic.Anthropic(api_key=key, max_retries=4)
        with st.status("Working on it…", expanded=False) as status:
            turn = analyst.run(client, secret("MODEL", "claude-haiku-5-5"), st.session_state["layer"], question,
                               history, on_step=lambda m: status.write(m))
            status.update(label="Done", state="complete")
        turn["question"] = question
        history.append(turn)
        st.rerun()   # redraw from history: clean layout, samples hidden, counter current
