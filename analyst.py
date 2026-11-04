"""Natural-language analytics on the governed semantic layer.

The AI model plans governed metric queries (query_metrics / list_values), then calls
present_answer with a headline, insights and chart specs that point at query results by id.
Every number comes from the semantic layer; charts are drawn by the app from those results.
"""
from __future__ import annotations

import json
import time

import prompts
from semantic_layer import QueryError, SemanticLayer

MAX_TURNS = 10
MAX_RESULT_CHARS = 8000

ANALYST_RULES = """You are a bank data analyst answering questions about FDIC-insured banks for business users.

How to work:
- Get every number from query_metrics. Never compute figures in your head beyond simple
  differences or ratios of numbers you retrieved, and never invent data.
- For trends, query a period range (e.g. 2024Q4:2026Q2) with dimension "period". Quarterly
  income is not available for 2024Q3, so start income trends at 2024Q4 unless the user asks for
  year-to-date figures.
- For comparisons across segments (region, size class, regulator, charter type, state,
  community-bank flag, specialization), use dimensions. Combine "period" with one segment
  dimension for "trend by segment" questions.
- Each query_metrics result carries a result_id (q1, q2, ...). Charts must point at one of them.
- If the data cannot answer the question, call present_answer with answer_type "decline" and
  explain what the data does cover.
- Answer the business question; do not ask the user follow-up questions.

When done, call present_answer exactly once:
- headline: one sentence that answers the question, with the key number and its unit.
- insights: 2 to 4 short sentences, each stating a specific observation backed by numbers from
  the results (largest/smallest, change over time, gaps between segments, notable outliers,
  data caveats such as the "Unknown" group). Do not speculate about causes.
- charts: 0 to 2 charts. Use "line" when x is "period", "bar" for comparing segments.
  y is one metric from that result; color is an optional second dimension in that result."""

PRESENT_TOOL = {
    "name": "present_answer",
    "description": "Present the final answer to the user. Call exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer_type": {"type": "string", "enum": ["answer", "decline"]},
            "headline": {"type": "string"},
            "insights": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "charts": {
                "type": "array", "maxItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "result_id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["line", "bar"]},
                        "x": {"type": "string", "description": "A dimension in that result (e.g. period, region)."},
                        "y": {"type": "string", "description": "A metric in that result."},
                        "color": {"type": "string", "description": "Optional second dimension in that result."},
                        "title": {"type": "string"},
                    },
                    "required": ["result_id", "kind", "x", "y", "title"],
                },
            },
            "decline_reason": {"type": "string"},
        },
        "required": ["answer_type", "headline"],
    },
}


def build_spec() -> dict:
    """System prompt and tools: analyst rules + the same governed glossary as Setup C."""
    c = prompts.build("C")
    system = f"{ANALYST_RULES}\n\nThe data covers 2024Q3 through 2026Q2.\n\n{prompts.render_glossary()}"
    tools = [t for t in c["tools"] if t["name"] in ("query_metrics", "list_values")] + [PRESENT_TOOL]
    return {"system": system, "tools": tools}


def history_messages(history: list[dict]) -> list[dict]:
    """Earlier turns as compact text, so follow-up questions have context."""
    msgs = []
    for turn in history[-4:]:
        msgs.append({"role": "user", "content": turn["question"]})
        a = turn.get("answer") or {}
        summary = a.get("headline", "") + ("\n" + "\n".join(a.get("insights", [])) if a.get("insights") else "")
        queries = "; ".join(json.dumps(r["request"]) for r in turn.get("results", {}).values())
        msgs.append({"role": "assistant", "content": f"{summary}\n(Queries used: {queries})" if queries else summary or "(no answer)"})
    return msgs


def run(client, model: str, layer: SemanticLayer, question: str, history: list[dict] | None = None,
        on_step=None, max_tokens: int = 4096) -> dict:
    """Answer one question. Returns {answer, results, steps, usage, latency_s, error}."""
    spec = build_spec()
    system = [{"type": "text", "text": spec["system"], "cache_control": {"type": "ephemeral"}}]
    tools = [dict(t) for t in spec["tools"]]
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    messages = history_messages(history or []) + [{"role": "user", "content": question}]
    results, steps, answer, error = {}, [], None, None
    usage = {"input": 0, "output": 0, "cache_read": 0}
    t0 = time.time()

    for _ in range(MAX_TURNS):
        try:
            resp = client.messages.create(model=model, max_tokens=max_tokens, system=system, tools=tools, messages=messages)
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {str(e)[:200]}"
            break
        usage["input"] += resp.usage.input_tokens
        usage["output"] += resp.usage.output_tokens
        usage["cache_read"] += getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            messages.append({"role": "user", "content": "Call present_answer now."})
            continue
        out = []
        for tu in uses:
            if tu.name == "present_answer":
                answer = tu.input
                out.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Shown to the user."})
                continue
            try:
                if tu.name == "query_metrics":
                    res = layer.query_metrics(**tu.input)
                    rid = f"q{len(results) + 1}"
                    results[rid] = {"request": tu.input, **res}
                    shown = {"result_id": rid, **{k: v for k, v in res.items() if k != "sql"}}
                    content, is_err = json.dumps(shown, default=str)[:MAX_RESULT_CHARS], False
                    if on_step:
                        on_step(f"Queried {', '.join(tu.input.get('metrics', []))}"
                                + (f" by {', '.join(tu.input['dimensions'])}" if tu.input.get("dimensions") else "")
                                + f" for {tu.input.get('period')}")
                elif tu.name == "list_values":
                    content, is_err = json.dumps(layer.list_values(**tu.input), default=str)[:MAX_RESULT_CHARS], False
                    if on_step:
                        on_step(f"Looked up {tu.input.get('dimension')} values")
                else:
                    content, is_err = f"ERROR: unknown tool {tu.name}", True
            except QueryError as e:
                content, is_err = f"ERROR: {e}", True
                if on_step:
                    on_step("A request was outside the governed metrics; adjusting")
            except TypeError as e:
                content, is_err = f"ERROR: bad arguments: {e}", True
            steps.append({"tool": tu.name, "input": tu.input, "error": is_err})
            out.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_err})
        messages.append({"role": "user", "content": out})
        if answer is not None:
            break

    if answer is None and error is None:
        error = "The model did not finish an answer."
    return {"answer": answer, "results": results, "steps": steps, "usage": usage,
            "latency_s": round(time.time() - t0, 1), "error": error}


def valid_charts(answer: dict, results: dict) -> list[dict]:
    """Keep only chart specs whose result, x, y and color exist in the data."""
    good = []
    for c in (answer or {}).get("charts") or []:
        r = results.get(c.get("result_id"))
        if not r or not r["rows"]:
            continue
        cols = set(r["rows"][0])
        if c.get("x") in cols and c.get("y") in cols and (not c.get("color") or c["color"] in cols):
            good.append(c)
    return good
