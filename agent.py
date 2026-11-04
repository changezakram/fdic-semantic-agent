"""The agent loop: one question, one arm, one attempt.

The model gets the arm's system prompt and tools, calls tools until it calls
final_answer, and every step is recorded for the run log.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import duckdb

from semantic_layer import QueryError, SemanticLayer

ROOT = Path(__file__).parent
SNAPSHOT = ROOT / "data" / "snapshot_2026Q2"
MAX_RESULT_CHARS = 8000
SQL_ROW_LIMIT = 50


class RawDatabase:
    """Arms A and B: the two raw tables, nothing else. Read-only."""

    def __init__(self):
        self.con = duckdb.connect()
        for t in ("raw_financials", "raw_institutions"):
            self.con.execute(f"CREATE TABLE {t} AS SELECT * FROM read_csv_auto('{SNAPSHOT / (t + '.csv')}')")

    def run_sql(self, sql: str) -> str:
        stmt = sql.strip().rstrip(";")
        if ";" in stmt:
            return "ERROR: run one statement at a time."
        if not re.match(r"(?is)^\s*(select|with|describe|show|summarize|pragma\s+table_info)\b", stmt):
            return "ERROR: read-only access. Only SELECT / WITH / DESCRIBE queries are allowed."
        try:
            cur = self.con.execute(stmt)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(SQL_ROW_LIMIT + 1)
        except Exception as e:  # noqa: BLE001 - errors go back to the model
            return f"ERROR: {str(e).splitlines()[0][:500]}"
        more = len(rows) > SQL_ROW_LIMIT
        rows = rows[:SQL_ROW_LIMIT]
        out = json.dumps({"columns": cols, "rows": rows, "truncated": more}, default=str)
        return out[:MAX_RESULT_CHARS]


class Tools:
    """Executes tool calls for one arm."""

    def __init__(self, arm: str, raw_db: RawDatabase | None = None, semantic: SemanticLayer | None = None):
        self.arm = arm
        self.raw_db = raw_db
        self.semantic = semantic

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Return (result text, is_error)."""
        try:
            if name == "run_sql" and self.arm in ("A", "B"):
                out = self.raw_db.run_sql(args.get("sql", ""))
                return out, out.startswith("ERROR")
            if name == "query_metrics" and self.arm == "C":
                res = self.semantic.query_metrics(**args)
                res = {k: v for k, v in res.items() if k != "sql"}      # the agent never sees SQL
                return json.dumps(res, default=str)[:MAX_RESULT_CHARS], False
            if name == "list_values" and self.arm == "C":
                return json.dumps(self.semantic.list_values(**args), default=str)[:MAX_RESULT_CHARS], False
            return f"ERROR: tool '{name}' is not available.", True
        except QueryError as e:
            return f"ERROR: {e}", True
        except TypeError as e:
            return f"ERROR: bad arguments for {name}: {e}", True


def run_question(client, model: str, spec: dict, tools: Tools, question: str,
                 max_turns: int = 12, max_tokens: int = 4096, temperature: float | None = None) -> dict:
    """Run one question. Returns a record for the run log."""
    system = [{"type": "text", "text": spec["system"], "cache_control": {"type": "ephemeral"}}]
    tool_defs = [dict(t) for t in spec["tools"]]
    tool_defs[-1] = {**tool_defs[-1], "cache_control": {"type": "ephemeral"}}
    messages = [{"role": "user", "content": question}]
    steps, usage = [], {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    final, nudged, error = None, False, None
    t0 = time.time()

    for turn in range(max_turns):
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, tools=tool_defs, messages=messages)
        if temperature is not None:
            kwargs["temperature"] = temperature
        try:
            resp = client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            error = f"{type(e).__name__}: {str(e)[:300]}"
            break
        u = resp.usage
        usage["input"] += u.input_tokens
        usage["output"] += u.output_tokens
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        if text:
            steps.append({"type": "text", "text": text[:2000]})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            if nudged:
                break
            nudged = True
            messages.append({"role": "user", "content": "Call final_answer now with your answer, or decline."})
            continue

        results = []
        for tu in tool_uses:
            if tu.name == "final_answer":
                final = tu.input
                steps.append({"type": "final_answer", "input": tu.input})
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Recorded."})
                continue
            out, is_err = tools.call(tu.name, tu.input)
            steps.append({"type": "tool", "name": tu.name, "input": tu.input, "output": out[:3000], "error": is_err})
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_err})
        messages.append({"role": "user", "content": results})
        if final is not None:
            break

    return {
        "final_answer": final,
        "steps": steps,
        "turns": turn + 1,
        "tool_calls": sum(1 for s in steps if s["type"] == "tool"),
        "tool_errors": sum(1 for s in steps if s["type"] == "tool" and s["error"]),
        "usage": usage,
        "latency_s": round(time.time() - t0, 2),
        "error": error or (None if final is not None else "no final_answer"),
    }
