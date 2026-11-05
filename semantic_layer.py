"""Arm C: the semantic layer.

The agent in Arm C never writes SQL. It calls two tools:

  query_metrics(metrics, period, dimensions=None, filters=None, basis=None,
                compare_to=None, order_by=None, limit=None)
  list_values(dimension, search=None)

This module compiles those calls into SQL using ONLY the definitions in
semantic_model.yaml, runs them on the frozen snapshot, and returns results with
units attached. Anything the model does not define is refused with a reason.

    from semantic_layer import SemanticLayer
    sl = SemanticLayer()
    sl.query_metrics(metrics=["net_income"], period="2025Q3")
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).parent
MODEL_FILE = ROOT / "semantic_model.yaml"
SNAPSHOT = ROOT / "data" / "snapshot_2026Q2"
EXT_SNAPSHOT = ROOT / "data" / "snapshot_2026Q2_ext"      # app-only extra fields, merger history, SOD
MAX_ROWS = 60   # all 50 states + DC fit in one result (was 50 in the frozen experiment)

# Dimensions whose values are names (matched by search); the rest are fixed categories.
NAME_DIMENSIONS = {"bank_name", "holding_company", "organization", "state_name"}


class QueryError(Exception):
    """A request the semantic layer refuses. The message is returned to the agent."""


class SemanticLayer:
    def __init__(self, model_file: Path = MODEL_FILE, snapshot: Path = SNAPSHOT, ext: Path | None = None,
                 pro_forma: bool = False):
        """Defaults reproduce the frozen experiment's layer (the tests use them). The app passes
        ext=EXT_SNAPSHOT and pro_forma=True: extra metrics, and every acquired bank's history restated to its
        acquirer (see extensions in the model). Metrics marked extension: true exist only with ext."""
        self.model = yaml.safe_load(Path(model_file).read_text())
        self.version = self.model["version"]
        self.pro_forma = pro_forma
        self.con = duckdb.connect()
        self.con.execute(f"CREATE TABLE raw_financials AS SELECT * FROM read_csv_auto('{snapshot / 'raw_financials.csv'}')")
        self.con.execute(f"CREATE TABLE raw_institutions AS SELECT * FROM read_csv_auto('{snapshot / 'raw_institutions.csv'}')")
        sql = self.model["base_view"]["sql"]
        self.has_ext = bool(ext and (Path(ext) / "fin_extra.csv").exists())
        if self.has_ext:
            sql = self._extend(sql, Path(ext))
        elif pro_forma:
            raise QueryError("pro_forma needs the extended snapshot (merger history).")
        self.con.execute("CREATE VIEW bank_quarter AS " + sql)
        self.periods = [r[0] for r in self.con.execute(
            "SELECT DISTINCT period FROM bank_quarter ORDER BY 1").fetchall()]
        self.dims = self.model["dimensions"]
        self.metrics = {k: v for k, v in self.model["metrics"].items()
                        if self.has_ext or not (isinstance(v, dict) and v.get("extension"))}
        self.measures = self.model["measures"]

    # ------------------------------------------------------------------ extended snapshot (app only)
    def _extend(self, sql: str, ext: Path) -> str:
        """Add the extra call-report fields, quarterly versions of the new income fields, and (when
        pro_forma) the merger restatement, without changing the frozen base view text in the model."""
        self.con.execute(f"CREATE TABLE fin_extra_raw AS SELECT * FROM read_csv_auto('{ext / 'fin_extra.csv'}')")
        have = {r[0] for r in self.con.execute("DESCRIBE raw_financials").fetchall()}
        cols = [r[0] for r in self.con.execute("DESCRIBE fin_extra_raw").fetchall()
                if r[0] not in have and r[0] not in ("CERT", "REPDTE")]
        self.con.execute("CREATE VIEW fin_extra AS SELECT CERT, REPDTE, " + ", ".join(cols) + " FROM fin_extra_raw")
        if "FROM raw_financials\n" not in sql:
            raise QueryError("Base view changed: cannot attach the extra fields.")
        sql = sql.replace("FROM raw_financials\n", "FROM raw_financials LEFT JOIN fin_extra USING (CERT, REPDTE)\n", 1)
        decum = "".join(
            f",\n      CASE WHEN f.REPDTE = 20240930 THEN NULL ELSE f.{c} - COALESCE(LAG(f.{c}) OVER w, 0) END AS {c}_Q"
            for c in ("EDEP", "EINTEXP", "INTINC"))
        sql = sql.replace("ELSE f.NTLNLS - COALESCE(LAG(f.NTLNLS) OVER w, 0) END AS NTLNLS_Q",
                          "ELSE f.NTLNLS - COALESCE(LAG(f.NTLNLS) OVER w, 0) END AS NTLNLS_Q" + decum, 1)
        mh = ext / "merger_history.csv"
        cfg = self.model.get("extensions", {}).get("pro_forma", {})
        codes = ", ".join(str(c) for c in cfg.get("change_codes", [221, 223, 224, 811]))
        cutoff = cfg.get("through", "2026-06-30")
        self.con.execute(f"""CREATE TABLE merger_events AS SELECT DISTINCT CAST(OUT_CERT AS BIGINT) AS OUT_CERT,
            CAST(ACQ_CERT AS BIGINT) AS ACQ_CERT, CAST(EFFDATE AS DATE) AS EFFDATE, OUT_INSTNAME, ACQ_INSTNAME
            FROM read_csv_auto('{mh}') WHERE CHANGECODE IN ({codes}) AND CAST(EFFDATE AS DATE) <= DATE '{cutoff}'
            AND OUT_CERT IS NOT NULL AND ACQ_CERT IS NOT NULL AND OUT_CERT <> ACQ_CERT""")
        # resolve chains (A -> B -> C) to the bank that survives today
        edges = dict(self.con.execute("SELECT OUT_CERT, ACQ_CERT FROM merger_events").fetchall())
        surv = {}
        for a in edges:
            b, seen = edges[a], {a}
            while b in edges and b not in seen:
                seen.add(b)
                b = edges[b]
            surv[a] = b
        self.con.execute("CREATE TABLE survivor (CERT BIGINT, SURV_CERT BIGINT)")
        if surv:
            self.con.executemany("INSERT INTO survivor VALUES (?, ?)", list(surv.items()))
        if self.pro_forma:
            sql = sql.replace("LEFT JOIN raw_institutions i USING (CERT)",
                              "LEFT JOIN survivor s ON s.CERT = f.CERT\n"
                              "    LEFT JOIN raw_institutions i ON i.CERT = COALESCE(s.SURV_CERT, f.CERT)", 1)
        return sql

    # ------------------------------------------------------------------ periods
    def _resolve_period(self, period, basis):
        """Return (list of periods, basis, is_full_year)."""
        if period is None:
            raise QueryError(f"period is required. Available quarters: {self.periods[0]} to {self.periods[-1]}.")
        p = str(period).strip().upper().replace(" ", "")
        full_year = False
        if re.fullmatch(r"\d{4}", p):                       # "2025" = full year = Q4 year-to-date
            periods, full_year = [f"{p}Q4"], True
            if basis == "quarterly":
                raise QueryError(f"'{p}' means full-year {p}. Use basis 'ytd' or omit basis, or ask for {p}Q4 for the fourth quarter alone.")
            basis = "ytd"
        elif re.fullmatch(r"\d{4}Q[1-4]", p):
            periods = [p]
        elif re.fullmatch(r"\d{4}Q[1-4][:\-]\d{4}Q[1-4]", p):
            a, b = re.split(r"[:\-]", p)
            if a not in self.periods or b not in self.periods:
                raise QueryError(f"Period range {p} is outside the data. Available quarters: {self.periods[0]} to {self.periods[-1]}.")
            periods = self.periods[self.periods.index(a): self.periods.index(b) + 1]
        else:
            raise QueryError(f"Unrecognized period '{period}'. Use 'YYYYQn' (e.g. 2026Q2), 'YYYY' for a full year, or a range 'YYYYQn:YYYYQn'.")
        missing = [x for x in periods if x not in self.periods]
        if missing:
            raise QueryError(f"No data for {', '.join(missing)}. Available quarters: {self.periods[0]} to {self.periods[-1]}. "
                             "Forecasts and earlier history are out of scope.")
        return periods, basis, full_year

    def _shift(self, period, how):
        i = self.periods.index(period)
        if how == "prior_quarter":
            target = self.periods[i - 1] if i > 0 else None
        elif how == "prior_year":
            y, q = int(period[:4]), period[4:]
            target = f"{y - 1}{q}"
        else:
            target = str(how).upper()
        if target not in self.periods:
            raise QueryError(f"Comparison period {target or how} is outside the data ({self.periods[0]} to {self.periods[-1]}).")
        return target

    # --------------------------------------------------------------- metric SQL
    def _metric_sql(self, name, basis, bank_filtered):
        if name not in self.metrics:
            raise QueryError(
                f"Unknown metric '{name}'. Available metrics: {', '.join(self.metrics)}. "
                f"Not available from this data: {'; '.join(self.model['out_of_scope'])}.")
        m = self.metrics[name]
        t = m["type"]
        if t == "count":
            return m["sql"], "count"
        if t == "sum":
            meas = self.measures[m["measure"]]
            col = meas["sql"]
            if meas["kind"] == "ytd_flow" and basis != "ytd":
                col = meas["quarterly_sql"]
            return f"SUM({col}) / 1e6", "usd_bn"
        if t == "ratio":
            return f"({m['numerator']}) / NULLIF({m['denominator']}, 0) * {m['scale']}", "pct"
        if t == "share":
            return "SUM(ASSET) * 100.0 / ANY_VALUE(_industry_assets)", "pct"
        if t == "bank_stat":
            fn = "AVG" if m["stat"] == "mean" else "MEDIAN"
            return f"{fn}({m['field']})", "pct"
        if t == "bank_field":
            if not bank_filtered:
                raise QueryError(f"'{name}' is a single bank's reported figure. Add filters={{'bank': <CERT>}} for one bank. "
                                 "For a group, use the aggregate metric (e.g. return_on_assets) or a median_bank_* metric.")
            return f"ANY_VALUE({m['field']})", "pct"
        raise QueryError(f"Metric '{name}' has an unsupported type '{t}'.")

    # ------------------------------------------------------------------ filters
    def _match_values(self, dim, wanted):
        expr = self.dims[dim]["sql"]
        values = [r[0] for r in self.con.execute(f"SELECT DISTINCT {expr} FROM bank_quarter").fetchall()]
        lower = {str(v).lower(): v for v in values}
        out = []
        for w in wanted:
            w_s = str(w).strip().lower()
            if w_s in lower:
                out.append(lower[w_s]); continue
            hits = sorted({v for v in values if w_s in str(v).lower()})
            if len(hits) == 1 and dim in NAME_DIMENSIONS:
                out.append(hits[0]); continue
            if len(hits) > 1:
                raise QueryError(f"'{w}' matches several {dim} values: {hits[:10]}. Use one exact value.")
            sample = sorted(map(str, values))[:25]
            hint = "Use list_values to search." if len(values) > 25 else f"Valid values: {sample}."
            raise QueryError(f"No {dim} matches '{w}'. {hint}")
        return out

    def _filter_sql(self, filters):
        clauses, bank_single = [], False
        for dim, val in (filters or {}).items():
            vals = val if isinstance(val, list) else [val]
            if dim in ("bank", "cert"):
                try:
                    certs = [int(v) for v in vals]
                except (TypeError, ValueError):
                    raise QueryError("The bank filter takes FDIC CERT numbers. Use list_values('bank_name', '<name>') to find one.")
                clauses.append(f"CERT IN ({', '.join(map(str, certs))})")
                bank_single = len(certs) == 1
                continue
            if dim not in self.dims:
                raise QueryError(f"Unknown filter '{dim}'. Available: bank (CERT), {', '.join(self.dims)}.")
            matched = self._match_values(dim, vals)
            lit = ", ".join("'" + str(v).replace("'", "''") + "'" for v in matched)
            clauses.append(f"({self.dims[dim]['sql']}) IN ({lit})")
        return clauses, bank_single

    # ------------------------------------------------------------- main entry
    def query_metrics(self, metrics, period, dimensions=None, filters=None, basis=None,
                      compare_to=None, order_by=None, limit=None):
        if isinstance(metrics, str):
            metrics = [metrics]
        if not metrics:
            raise QueryError("Ask for at least one metric.")
        dimensions = list(dimensions or [])
        if basis not in (None, "quarterly", "ytd"):
            raise QueryError("basis must be 'quarterly' (default: earned in the quarter) or 'ytd' (year-to-date).")
        periods, basis, full_year = self._resolve_period(period, basis)
        basis = basis or "quarterly"

        flow_metrics = [m for m in metrics if self.metrics.get(m, {}).get("type") == "sum"
                        and self.measures[self.metrics[m]["measure"]]["kind"] == "ytd_flow"]
        if flow_metrics and basis == "quarterly" and "2024Q3" in periods:
            raise QueryError("Quarterly income for 2024Q3 needs 2024Q2 year-to-date figures, which are outside the data. "
                             "Use basis 'ytd' for 2024Q3, or a later quarter.")

        for d in dimensions:
            if d not in self.dims:
                raise QueryError(f"Unknown dimension '{d}'. Available: {', '.join(self.dims)}.")
        if len(periods) > 1 and "period" not in dimensions:
            dimensions = ["period"] + dimensions

        where, bank_single = self._filter_sql(filters)
        compiled = {m: self._metric_sql(m, basis, bank_single) for m in metrics}

        if compare_to and dimensions:
            return self._compare_by_group(metrics, periods, dimensions, filters, basis, full_year, flow_metrics,
                                          compare_to, order_by, limit)
        if compare_to:
            if len(periods) != 1:
                raise QueryError("compare_to works with one period.")
            base = periods[0]
            other = self._shift(base, compare_to)
            if flow_metrics and basis == "quarterly" and other == "2024Q3":
                raise QueryError("Quarterly income for 2024Q3 is not available (needs 2024Q2). Compare year-to-date instead.")
            cur = self._run(compiled, [base], [], where)
            prev = self._run(compiled, [other], [], where)
            rows = []
            for m, (_, unit) in compiled.items():
                a, b = cur["rows"][0][m], prev["rows"][0][m]
                row = {"metric": m, "unit": unit, base: a, other: b}
                if a is not None and b is not None:
                    if unit == "pct":
                        row["change_pp"] = round(a - b, 4)
                    else:
                        row["change"] = round(a - b, 4)
                        row["change_pct"] = round((a / b - 1) * 100, 4) if b else None
                rows.append(row)
            return self._result(rows, metrics, basis, full_year, cur["sql"] + "\n-- vs --\n" + prev["sql"],
                                note=f"Compared {base} with {other}. Each period uses all banks that filed that quarter.")

        out = self._run(compiled, periods, dimensions, where, order_by, limit)
        return self._result(out["rows"], metrics, basis, full_year, out["sql"], truncated=out["truncated"])

    SIZE_DIMS = ("size_class", "organization_size_class", "organization_asset_band")

    def _compare_by_group(self, metrics, periods, dimensions, filters, basis, full_year, flow_metrics,
                          compare_to, order_by, limit):
        """Change by group: one metric, one dimension, two periods. Groups (and any size filter) are set by the
        later period; the earlier period is measured for those same groups. Each group also reports whether its
        set of banks changed between the periods (usually a merger), so growth is not mistaken for organic."""
        if len(periods) != 1 or len(dimensions) != 1 or dimensions[0] == "period" or len(metrics) != 1:
            raise QueryError("compare_to with dimensions takes one period, one dimension and one metric.")
        m, d, base = metrics[0], dimensions[0], periods[0]
        other = self._shift(base, compare_to)
        if flow_metrics and basis == "quarterly" and other == "2024Q3":
            raise QueryError("Quarterly income for 2024Q3 is not available (needs 2024Q2). Compare year-to-date instead.")
        where_cur, single = self._filter_sql(filters)
        prior_filters = {k: v for k, v in (filters or {}).items() if k not in self.SIZE_DIMS}
        where_prev, _ = self._filter_sql(prior_filters)
        compiled = {m: self._metric_sql(m, basis, single), "_banks": ("COUNT(DISTINCT CERT)", "count")}
        cur = self._run(compiled, [base], [d], where_cur, limit=10000)
        prev = self._run(compiled, [other], [d], where_prev, limit=10000)
        before = {r[d]: r for r in prev["rows"]}
        unit = compiled[m][1]
        rows = []
        for r in cur["rows"]:
            p = before.get(r[d], {})
            a, b = r.get(m), p.get(m)
            row = {d: r[d], m: a, f"{m}_{other}": b}
            if a is not None and b is not None:
                if unit == "pct":
                    row["change_pp"] = round(a - b, 4)
                else:
                    row["change"] = round(a - b, 4)
                    row["change_pct"] = round((a / b - 1) * 100, 4) if b else None
            if d in ("organization", "holding_company") and not self.pro_forma:
                row["banks_changed"] = p.get("_banks") != r.get("_banks")
            rows.append(row)
        key = str(order_by).split()[0] if order_by else ("change_pct" if unit != "pct" else "change_pp")
        desc = not (order_by and str(order_by).lower().endswith(" asc"))
        rows.sort(key=lambda x: (x.get(key) is None, -(x.get(key) or 0) if desc else (x.get(key) or 0)))
        cap = min(int(limit), MAX_ROWS) if limit else MAX_ROWS
        truncated = len(rows) > cap
        rows = rows[:cap]
        note = f"Compared {base} with {other} for each {d.replace('_', ' ')}. Groups are set by {base}."
        if self.pro_forma:
            note += (" Pro forma for mergers: banks acquired before the later period are counted with their "
                     "acquirer in both periods, so changes are like for like.")
        elif d in ("organization", "holding_company"):
            note += (" banks_changed = the organization's own banks differ between the periods (a merger), so its "
                     "change is not organic. Not yet detected: buying a bank that sat under another holding company "
                     "(merger history is not loaded). Over a year, treat large jumps as possible acquisitions; "
                     "prior_quarter is less exposed.")
        res = self._result(rows, [m], basis, full_year, cur["sql"] + "\n-- vs --\n" + prev["sql"],
                           truncated=truncated, note=note)
        u = res["units"][m]
        res["units"].update({f"{m}_{other}": u, "change": u, "change_pct": "percent", "change_pp": "percent"})
        return res

    def _run(self, compiled, periods, dimensions, where, order_by=None, limit=None):
        sel_dims, group = [], []
        for d in dimensions:
            spec = self.dims[d]
            sel_dims.append(f"{spec['sql']} AS \"{d}\"")
            group.append(spec.get("key_sql", spec["sql"]))
            if "key_sql" in spec:
                group.append(spec["sql"])
        sel_metrics = [f"{sql} AS \"{m}\"" for m, (sql, _) in compiled.items()]
        plist = ", ".join(f"'{p}'" for p in periods)
        conds = [f"period IN ({plist})"] + where
        needs_share = any(self.metrics.get(m, {}).get("type") == "share" for m in compiled)
        src = "bank_quarter"
        if needs_share:
            src = ("(SELECT b.*, t._industry_assets FROM bank_quarter b JOIN "
                   "(SELECT period AS _p, SUM(ASSET) AS _industry_assets FROM bank_quarter GROUP BY 1) t ON t._p = b.period)")
        sql = f"SELECT {', '.join(sel_dims + sel_metrics)}\nFROM {src}\nWHERE {' AND '.join(conds)}"
        if group:
            sql += f"\nGROUP BY {', '.join(dict.fromkeys(group))}"
        if order_by:
            col, _, direction = str(order_by).partition(" ")
            if col not in compiled and col not in dimensions:
                raise QueryError(f"order_by must name a requested metric or dimension, optionally followed by 'asc' or 'desc'.")
            sql += f"\nORDER BY \"{col}\" {'ASC' if direction.lower() == 'asc' else 'DESC'} NULLS LAST"
        elif "period" in dimensions:
            sql += '\nORDER BY "period"'
        cap = int(limit) if limit and int(limit) > MAX_ROWS else (min(int(limit), MAX_ROWS) if limit else MAX_ROWS)
        rows = self.con.execute(sql + f"\nLIMIT {cap + 1}").df().to_dict("records")
        truncated = len(rows) > cap
        rows = [{k: (None if v != v else v) for k, v in r.items()} for r in rows[:cap]]   # NaN -> None
        return {"rows": rows, "sql": sql, "truncated": truncated}

    def _result(self, rows, metrics, basis, full_year, sql, truncated=False, note=None):
        units = {m: self._metric_sql(m, basis, True)[1] for m in metrics}
        notes = []
        if any(self.metrics[m]["type"] == "sum" and self.measures[self.metrics[m]["measure"]]["kind"] == "ytd_flow" for m in metrics):
            notes.append("Income amounts are full-year (Q4 year-to-date)." if full_year else
                         "Income amounts are earned in the quarter." if basis == "quarterly" else
                         "Income amounts are year-to-date.")
        if any(self.metrics[m]["type"] == "ratio" for m in metrics):
            notes.append("Ratios are aggregate (sum of numerator / sum of denominator), income annualized, on quarter-end balances.")
        if note:
            notes.append(note)
        if truncated:
            notes.append(f"Only the first {MAX_ROWS} rows are shown; add filters or a limit.")
        for r in rows:
            for k, v in list(r.items()):
                if isinstance(v, float):
                    r[k] = round(v, 4)
        return {
            "rows": rows,
            "units": {m: {"usd_bn": "$ billions", "pct": "percent", "count": "count"}[u] for m, u in units.items()},
            "notes": notes,
            "semantic_model_version": self.version,
            "sql": sql,
        }

    # ------------------------------------------------------------- list_values
    def list_values(self, dimension, search=None):
        if dimension not in self.dims:
            raise QueryError(f"Unknown dimension '{dimension}'. Available: {', '.join(self.dims)}.")
        spec = self.dims[dimension]
        label = spec.get("label_sql", spec["sql"])
        key = "CERT" if dimension in ("bank", "bank_name") else spec.get("key_sql", spec["sql"])
        sql = f"SELECT {key} AS key, ANY_VALUE({label}) AS value, COUNT(DISTINCT CERT) AS banks FROM bank_quarter WHERE period = '{self.periods[-1]}'"
        if search:
            s = str(search).lower().replace("'", "''")
            sql += f" AND (LOWER({label}) LIKE '%{s}%' OR CAST({key} AS VARCHAR) = '{s}')"
        sql += " GROUP BY 1 ORDER BY banks DESC, value LIMIT 25"
        rows = self.con.execute(sql).df().to_dict("records")
        if dimension in ("bank", "bank_name"):
            for r in rows:
                r["cert"] = r.pop("key")
                r.pop("banks")
        else:
            for r in rows:
                r.pop("key")
        return {"dimension": dimension, "description": spec.get("description", ""), "values": rows,
                "note": "Values as of the latest quarter. Up to 25 shown."}


if __name__ == "__main__":
    import json
    sl = SemanticLayer()
    print(json.dumps(sl.query_metrics(["net_income", "return_on_assets"], "2026Q2"), indent=2, default=str))
