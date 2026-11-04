"""Prompts and tools for the three arms, all rendered from one source.

  Arm A: raw schema (table and column names, types) + run_sql
  Arm B: raw schema + governed glossary + run_sql
  Arm C: governed glossary + query_metrics / list_values (no SQL)

Arms B and C get the IDENTICAL glossary text, rendered from semantic_model.yaml.
The only difference between B and C is who applies the definitions: the model
(writing SQL) or code (the semantic layer). Every arm ends by calling final_answer.

    python prompts.py      # writes prompts/arm_A.md, arm_B.md, arm_C.md and prints their hashes
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).parent
MODEL = yaml.safe_load((ROOT / "semantic_model.yaml").read_text())
SNAPSHOT = ROOT / "data" / "snapshot_2026Q2"
PROMPT_VERSION = "1"

# --------------------------------------------------------------------------- common
COMMON = """You are a data analyst answering questions about U.S. banks using FDIC call report data.
The data covers FDIC-insured banks for the quarters 2024Q3 through 2026Q2.

Answer using only data you retrieve with your tools. Work step by step: look at the data as
needed, compute the answer, then call final_answer exactly once. If the question cannot be
answered from this data, call final_answer with answer_type "decline" and say why.
Do not ask the user follow-up questions; make the most reasonable interpretation."""

FINAL_ANSWER_TOOL = {
    "name": "final_answer",
    "description": "Submit your final answer. Call this exactly once, at the end.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answer_type": {"type": "string", "enum": ["number", "label", "list", "decline"],
                            "description": "number for an amount, ratio, count or change; label for a single name or period; list for several names; decline if the data cannot answer the question."},
            "value": {"description": "The answer: a number (no commas or symbols), a string, or a list of strings. Omit when declining.",
                      "anyOf": [{"type": "number"}, {"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "unit": {"type": "string",
                     "enum": ["usd", "usd_thousands", "usd_millions", "usd_billions", "usd_trillions",
                              "percent", "percentage_points", "count", "none"],
                     "description": "Unit of a number answer. Use none for labels, lists and declines."},
            "explanation": {"type": "string", "description": "One or two sentences on how you got the answer, or why you declined."},
        },
        "required": ["answer_type", "unit", "explanation"],
    },
}

# --------------------------------------------------------------------------- arm A/B pieces
RUN_SQL_TOOL = {
    "name": "run_sql",
    "description": "Run one read-only SQL query (DuckDB dialect) against the database and get up to 50 rows back.",
    "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
}


def render_schema() -> str:
    """Table and column names with types only. No descriptions."""
    con = duckdb.connect()
    out = ["## Database schema", ""]
    for table in ("raw_financials", "raw_institutions"):
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv_auto('{SNAPSHOT / (table + '.csv')}') LIMIT 0")
        cols = con.execute(f"DESCRIBE {table}").fetchall()
        out.append(f"{table}(" + ", ".join(f"{c[0]} {c[1]}" for c in cols) + ")")
    return "\n".join(out)


# --------------------------------------------------------------------------- glossary (B and C)
def _block(text) -> str:
    return " ".join(str(text).split())


def render_glossary() -> str:
    m = MODEL
    L = [f"## Governed definitions (semantic model v{m['version']})",
         "These are the official business definitions. Follow them exactly.", ""]

    L.append("### Conventions")
    for key, val in m["conventions"].items():
        if isinstance(val, dict):
            for sub, text in val.items():
                if isinstance(text, dict):
                    text = "; ".join(f"{k}: {v}" for k, v in text.items())
                L.append(f"- {key}.{sub}: {_block(text)}")
        else:
            L.append(f"- {key}: {_block(val)}")
    L.append("")

    L.append("### Derived terms used in formulas")
    for key, val in m["derived_terms"].items():
        L.append(f"- {key}: {_block(val)}")
    L.append("")

    L.append("### Ontology")
    for name, e in m["ontology"]["entities"].items():
        desc = _block(e.get("description", ""))
        if "values" in e:
            desc += " Values: " + "; ".join(f"{k} = {v}" for k, v in e["values"].items())
        L.append(f"- {name} (key {e['key']}): {desc}")
    L.append("- Relationships: " + "; ".join(m["ontology"]["relationships"]))
    L.append("- State to FDIC region: " + "; ".join(
        f"{r}: {', '.join(s)}" for r, s in m["ontology"]["state_to_region"].items()))
    L.append("")

    L.append("### Dimensions (ways to group or filter)")
    for name, d in m["dimensions"].items():
        line = f"- {name}: {_block(d['sql'])}"
        if d.get("description"):
            line += f". {_block(d['description'])}"
        L.append(line)
    L.append("")

    L.append("### Measures (raw fields)")
    for name, ms in m["measures"].items():
        kind = {"stock": "point-in-time balance", "ytd_flow": "year-to-date flow"}[ms["kind"]]
        line = f"- {name} = {ms['sql']} ({kind}, $ thousands)"
        if ms.get("quarterly_sql"):
            line += f"; quarterly amount = {ms['quarterly_sql']}"
        if ms.get("description"):
            line += f". {_block(ms['description'])}"
        L.append(line)
    L.append("")

    L.append("### Metrics")
    for name, mt in m["metrics"].items():
        t = mt["type"]
        if t == "count":
            f = mt["sql"]
        elif t == "sum":
            f = f"sum of measure {mt['measure']}" + (" (quarterly by default; year-to-date on request)" if "bases" in mt else "")
        elif t in ("ratio", "share"):
            f = f"{_block(mt['numerator'])} / {_block(mt['denominator'])} x {mt['scale']}"
        elif t == "bank_stat":
            f = f"{mt['stat']} of bank-reported {mt['field']} across banks"
        else:
            f = f"one bank's reported {mt['field']} (requires a single bank)"
        line = f"- {name}: {f}"
        if mt.get("description"):
            line += f". {_block(mt['description'])}"
        L.append(line)
    L.append("")

    L.append("### Comparisons")
    for key, val in m["comparisons"].items():
        L.append(f"- {key}: {_block(val)}")
    L.append("")

    L.append("### Out of scope (decline these)")
    for item in m["out_of_scope"]:
        L.append(f"- {item}")
    return "\n".join(L)


# --------------------------------------------------------------------------- arm C tools
def arm_c_tools() -> list[dict]:
    m = MODEL
    return [
        {
            "name": "query_metrics",
            "description": (
                "Get governed metrics from the semantic layer. You cannot write SQL; this tool applies the "
                "official definitions (units, year-to-date rules, populations) for you. Dollar results are "
                "in $ billions; ratios in percent. Returns rows, units, and notes on the rules applied. "
                "Returns an error with a reason if the request cannot be answered from the data."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "metrics": {"type": "array", "items": {"type": "string", "enum": list(m["metrics"])},
                                "description": "One or more metric names."},
                    "period": {"type": "string",
                               "description": "'YYYYQn' for one quarter (e.g. 2026Q2), 'YYYY' for a full year, or 'YYYYQn:YYYYQn' for a range."},
                    "dimensions": {"type": "array", "items": {"type": "string", "enum": list(m["dimensions"])},
                                   "description": "Optional. Group results by these dimensions."},
                    "filters": {"type": "object",
                                "description": "Optional. {dimension: value or [values]}. Use 'bank' with an FDIC CERT number for one bank. Names are matched case-insensitively; use list_values to find exact values."},
                    "basis": {"type": "string", "enum": ["quarterly", "ytd"],
                              "description": "Optional, for income metrics. quarterly (default) = earned in the quarter; ytd = year-to-date."},
                    "compare_to": {"type": "string",
                                   "description": "Optional. 'prior_quarter', 'prior_year', or a 'YYYYQn' period. Returns both values and the change (percent change, or percentage-point change for ratios). One period, no dimensions."},
                    "order_by": {"type": "string", "description": "Optional. A requested metric or dimension, then 'asc' or 'desc' (e.g. 'total_assets desc')."},
                    "limit": {"type": "integer", "description": "Optional. Maximum rows (up to 50)."},
                },
                "required": ["metrics", "period"],
            },
        },
        {
            "name": "list_values",
            "description": "Look up valid values of a dimension, optionally searching by text (e.g. find a bank's CERT by name, or a holding company's exact name).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "dimension": {"type": "string", "enum": list(m["dimensions"])},
                    "search": {"type": "string", "description": "Optional text to search for."},
                },
                "required": ["dimension"],
            },
        },
    ]


# --------------------------------------------------------------------------- assemble
def build(arm: str) -> dict:
    arm = arm.upper()
    if arm == "A":
        system, tools = f"{COMMON}\n\n{render_schema()}", [RUN_SQL_TOOL]
    elif arm == "B":
        system, tools = f"{COMMON}\n\n{render_schema()}\n\n{render_glossary()}", [RUN_SQL_TOOL]
    elif arm == "C":
        system = (f"{COMMON}\n\nYou have no SQL access. Use query_metrics and list_values; they apply the "
                  f"definitions below for you.\n\n{render_glossary()}")
        tools = arm_c_tools()
    else:
        raise ValueError("arm must be A, B or C")
    tools = tools + [FINAL_ANSWER_TOOL]
    digest = hashlib.sha256(json.dumps({"system": system, "tools": tools}, sort_keys=True).encode()).hexdigest()
    return {"arm": arm, "system": system, "tools": tools, "prompt_version": PROMPT_VERSION,
            "semantic_model_version": MODEL["version"], "prompt_sha256": digest}


if __name__ == "__main__":
    out = ROOT / "prompts"
    out.mkdir(exist_ok=True)
    for arm in "ABC":
        spec = build(arm)
        text = spec["system"] + "\n\n## Tools\n\n" + json.dumps(spec["tools"], indent=2)
        (out / f"arm_{arm}.md").write_text(text)
        print(f"Arm {arm}: {len(spec['system']):>6,} chars, tools: {', '.join(t['name'] for t in spec['tools'])}; "
              f"sha256 {spec['prompt_sha256'][:12]}")
    print(f"Wrote prompts/arm_A.md, arm_B.md, arm_C.md (prompt v{PROMPT_VERSION}, semantic model v{MODEL['version']})")
