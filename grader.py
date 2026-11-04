"""Grade a final_answer against the answer key. Deterministic; no LLM judge.

Outcomes:
  correct        right answer (or a correct decline)
  wrong_value    a number, label or list that does not match
  wrong_units    the number would be right in a different dollar unit (the $ thousands trap)
  wrong_type     a non-number for a number question, or a number in a non-matching unit
  false_decline  declined an answerable question
  missed_decline answered a question it should have declined
  missing_value  answer_type given but the value field left empty
  no_answer      never called final_answer (or the API failed)

Grader v2 (2026-11-03): numbers rounded by the agent count as correct when they equal the
expected value rounded to the same decimals and keep at least 3 significant figures;
list names are compared after normalizing punctuation and abbreviations.
Grader v3 (2026-11-03): a dollar answer that follows the governed answer format
($ billions to 1 decimal, or $ trillions to 2 decimals) counts as correct when it equals the
expected value rounded that way (found in review: Arm C reported $0.305B as "$0.3B" per the format).
"""
from __future__ import annotations

import re

GRADER_VERSION = 3

TO_BILLIONS = {"usd": 1e-9, "usd_thousands": 1e-6, "usd_millions": 1e-3, "usd_billions": 1.0, "usd_trillions": 1e3}
PERIOD_RE = re.compile(r"(\d{4})\D{0,3}q([1-4])|q([1-4])\D{0,5}(\d{4})", re.I)
WORD_Q = {"first": "1", "second": "2", "third": "3", "fourth": "4"}


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            return float(s.split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _period(s):
    s = str(s).lower()
    for w, d in WORD_Q.items():
        s = s.replace(f"{w} quarter", f"q{d}")
    m = PERIOD_RE.search(s)
    if not m:
        return None
    return f"{m.group(1)}Q{m.group(2)}" if m.group(1) else f"{m.group(4)}Q{m.group(3)}"


def _decimals(x):
    s = repr(float(x))
    return 0 if "e" in s else len(s.split(".")[1].rstrip("0"))


def _sig_figs(x):
    digits = re.sub(r"[^0-9]", "", f"{abs(float(x)):.10f}".rstrip("0").rstrip(".")).lstrip("0")
    return len(digits)


def _within(got, exp, tol):
    if "relative" in tol and abs(got - exp) <= tol["relative"] * abs(exp):
        return True
    if "absolute" in tol and abs(got - exp) <= tol["absolute"]:
        return True
    d = _decimals(got)                     # the agent rounded: accept if it is exp rounded the same way
    return _sig_figs(got) >= 3 and abs(round(exp, d) - got) < 1e-9


def _norm(name):
    s = str(name).lower().replace("bcorp", "bancorp").replace("finl", "financial")
    return re.sub(r"[^a-z0-9]", "", s)


def grade(answer: dict | None, key: dict) -> dict:
    t = key["type"]
    if answer is None:
        return {"outcome": "no_answer", "correct": False}
    atype = answer.get("answer_type")
    if t == "decline":
        ok = atype == "decline"
        return {"outcome": "correct" if ok else "missed_decline", "correct": ok}
    if atype == "decline":
        return {"outcome": "false_decline", "correct": False}

    exp, tol, value, unit = key["expected"], key["tolerance"], answer.get("value"), answer.get("unit", "none")
    if value is None or value == "" or value == []:
        return {"outcome": "missing_value", "correct": False}

    if t in ("dollars_bn", "dollars_tn"):
        got = _num(value)
        if got is None or unit not in TO_BILLIONS:
            return {"outcome": "wrong_type", "correct": False}
        got_bn = got * TO_BILLIONS[unit]
        exp_bn = exp if t == "dollars_bn" else exp * 1000
        governed = (unit == "usd_billions" and abs(got - round(exp_bn, 1)) < 1e-9) or \
                   (unit == "usd_trillions" and abs(got - round(exp_bn / 1000, 2)) < 1e-9)
        if _within(got_bn, exp_bn, tol) or governed:
            return {"outcome": "correct", "correct": True, "normalized": got_bn}
        for factor in (1e3, 1e-3, 1e6, 1e-6):          # right digits, wrong unit
            if _within(got_bn * factor, exp_bn, tol):
                return {"outcome": "wrong_units", "correct": False, "normalized": got_bn}
        return {"outcome": "wrong_value", "correct": False, "normalized": got_bn}

    if t in ("ratio_pct", "growth_pct", "pp_change"):
        got = _num(value)
        allowed = {"percent", "percentage_points", "none"}
        if got is None or unit not in allowed:
            return {"outcome": "wrong_type", "correct": False}
        if _within(got, exp, tol):
            return {"outcome": "correct", "correct": True, "normalized": got}
        if _within(got * 100, exp, tol):              # gave a fraction instead of a percent
            return {"outcome": "wrong_units", "correct": False, "normalized": got}
        return {"outcome": "wrong_value", "correct": False, "normalized": got}

    if t == "count":
        got = _num(value)
        if got is None:
            return {"outcome": "wrong_type", "correct": False}
        ok = round(abs(got)) == abs(exp)
        return {"outcome": "correct" if ok else "wrong_value", "correct": ok, "normalized": got}

    if t == "label":
        got = value if isinstance(value, str) else (value[0] if isinstance(value, list) and value else str(value))
        if _period(exp):
            ok = _period(got) == _period(exp)
        else:
            ok = re.search(rf"\b{re.escape(str(exp).lower())}\b", str(got).lower()) is not None
        return {"outcome": "correct" if ok else "wrong_value", "correct": ok}

    if t == "list":
        items = value if isinstance(value, list) else re.split(r"[;\n]|,(?=\s*[A-Z])", str(value))
        items = [_norm(i) for i in items if str(i).strip()]
        aliases = key.get("aliases", {})

        def matches(item, name):
            return _norm(name) in item or any(_norm(a) in item for a in aliases.get(name, []))

        found = all(any(matches(i, name) for i in items) for name in exp)
        ok = found and len(items) == len(exp)
        return {"outcome": "correct" if ok else "wrong_value", "correct": ok}

    raise ValueError(f"unknown answer type {t}")
