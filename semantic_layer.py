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
        self._ext_path = Path(ext) if self.has_ext else None
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

    # ------------------------------------------------------------------ merger-quarter flags (app only)
    QUARTERLY_INCOME = {"return_on_assets_quarterly", "net_interest_margin", "cost_of_funds", "cost_of_deposits",
                        "cost_of_interest_bearing_deposits"}
    FLAG_DIMS = ("organization", "holding_company", "bank_name", "bank")

    def query_metrics(self, metrics, period, dimensions=None, filters=None, basis=None, compare_to=None,
                      order_by=None, limit=None):
        res = self._query_metrics_core(metrics, period, dimensions=dimensions, filters=filters, basis=basis,
                                       compare_to=compare_to, order_by=order_by, limit=limit)
        if self.has_ext and set(metrics or []) & self.QUARTERLY_INCOME:
            self._flag_merger_quarters(res, period, dimensions or [], filters or {})
        return res

    def _merger_flags(self, dim: str) -> set:
        """(group value, period) pairs for the quarter a merger closed and the quarter after. In those
        quarters call-report income can differ materially from reported results (merger charges, purchase
        accounting, year-to-date restatement), so quarterly income ratios carry a caveat."""
        if not hasattr(self, "_flag_cache"):
            self._flag_cache = {}
        if dim in self._flag_cache:
            return self._flag_cache[dim]
        sql_dim = self.model["dimensions"][dim]["sql"] if isinstance(self.model["dimensions"][dim], dict) else dim
        rows = self.con.execute(f"""
            WITH ev AS (SELECT ACQ_CERT AS ACQ, EFFDATE FROM merger_events),
            q AS (SELECT ACQ, (year(EFFDATE) || 'Q' || quarter(EFFDATE)) AS p0,
                         (year(EFFDATE + INTERVAL 3 MONTH) || 'Q' || quarter(EFFDATE + INTERVAL 3 MONTH)) AS p1 FROM ev)
            SELECT DISTINCT {sql_dim} AS g, b.period FROM bank_quarter b
            JOIN q ON COALESCE((SELECT SURV_CERT FROM survivor s WHERE s.CERT = q.ACQ), q.ACQ) = b.CERT
            WHERE b.period IN (q.p0, q.p1)""").fetchall()
        self._flag_cache[dim] = {(str(g), p) for g, p in rows}
        return self._flag_cache[dim]

    def _flag_merger_quarters(self, res, period, dimensions, filters):
        hit = set()
        dims = [d for d in dimensions if d in self.FLAG_DIMS]
        for d in dims:
            flags = self._merger_flags(d)
            for row in res.get("rows") or []:
                p = row.get("period", period if isinstance(period, str) and ":" not in str(period) else None)
                if p and (str(row.get(d)), p) in flags:
                    row["merger_quarter"] = True
                    hit.add(f"{row.get(d)} {p}")
        for d in self.FLAG_DIMS:                      # one organization or bank picked by filter
            if d in filters and not dims:
                vals = filters[d] if isinstance(filters[d], list) else [filters[d]]
                flags = self._merger_flags(d)
                for row in res.get("rows") or []:
                    p = row.get("period", period)
                    for v in vals:
                        if any(str(g).upper() == str(v).upper() and q == p for g, q in flags):
                            row["merger_quarter"] = True
                            hit.add(f"{v} {p}")
        if hit:
            res.setdefault("notes", []).append(
                "merger_quarter = a merger closed in this quarter or the one before. Quarterly income ratios from "
                "call reports can differ materially from reported results in these quarters: " + ", ".join(sorted(hit)))

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
    def _query_metrics_core(self, metrics, period, dimensions=None, filters=None, basis=None,
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

    # ------------------------------------------------------------- deposit market share (app only)
    def _load_sod(self):
        """Branch deposits from the FDIC Summary of Deposits (June 30 each year), each branch tagged with its
        banking organization: the same organization names as query_metrics and, when pro_forma, acquired
        banks counted with their acquirer in both years."""
        if getattr(self, "_sod_ready", False):
            return
        ext = getattr(self, "_ext_path", None)
        files = sorted(Path(ext).glob("sod_*.csv")) if ext else []
        if not files:
            raise QueryError("Deposit market share needs the Summary of Deposits data (not loaded).")
        self.con.execute("CREATE TABLE sod_raw AS SELECT * FROM read_csv_auto([" + ", ".join(f"'{f}'" for f in files)
                         + "], union_by_name = true)")
        self.con.execute("""CREATE TABLE cert_org AS SELECT CERT, arg_max(COALESCE(hc_name, NAME), period) AS organization
                            FROM bank_quarter GROUP BY CERT""")
        surv = "LEFT JOIN survivor s ON s.CERT = b.CERT" if self.pro_forma else ""
        key = "COALESCE(s.SURV_CERT, b.CERT)" if self.pro_forma else "b.CERT"
        self.con.execute(f"""CREATE TABLE sod AS
            SELECT b.YEAR AS year, b.CERT, b.STALPBR AS state, b.CNTYNAMB AS county, b.MSANAMB AS metro,
                   COALESCE(o.organization, b.NAMEHCR, b.NAMEFULL) AS organization,
                   b.DEPSUMBR AS deposits_k
            FROM sod_raw b {surv} LEFT JOIN cert_org o ON o.CERT = {key}""")
        self.sod_years = [r[0] for r in self.con.execute("SELECT DISTINCT year FROM sod ORDER BY 1").fetchall()]
        self._sod_ready = True

    def _resolve_market(self, market_type, market, year):
        col = {"state": "state", "metro": "metro", "county": "county"}.get(market_type)
        if not col:
            raise QueryError("market_type must be 'state', 'metro' (metropolitan statistical area) or 'county'.")
        m = str(market).strip()
        if col == "state":
            try:
                from charts import STATE_NAMES as states
            except Exception:  # noqa: BLE001
                states = {}
            code = next((k for k, v in states.items() if v.lower() == m.lower()), m.upper())
            if not self.con.execute("SELECT 1 FROM sod WHERE state = ? LIMIT 1", [code]).fetchone():
                raise QueryError(f"No branches found for state '{market}'. Use a two-letter code such as 'MA'.")
            return f"state = '{code}'", code
        if col == "county":
            name, _, st = m.partition(",")
            cands = self.con.execute("SELECT DISTINCT county, state FROM sod WHERE year = ? AND LOWER(county) LIKE ? "
                                     + ("AND state = ?" if st.strip() else ""),
                                     [year, f"%{name.strip().lower()}%"] + ([st.strip().upper()] if st.strip() else [])).fetchall()
            if len(cands) != 1:
                raise QueryError((f"'{market}' matches {len(cands)} counties: " + "; ".join(f"{c}, {s}" for c, s in cands[:12])
                                  if cands else f"No county matches '{market}'.") + " Give the county as 'Name, ST'.")
            c, st = cands[0]
            return f"county = '{c.replace(chr(39), chr(39) * 2)}' AND state = '{st}'", f"{c} County, {st}"
        cands = [r[0] for r in self.con.execute("SELECT DISTINCT metro FROM sod WHERE year = ? AND LOWER(metro) LIKE ?",
                                                 [year, f"%{m.lower()}%"]).fetchall()]
        exact = [c for c in cands if c.lower() == m.lower() or c.lower().startswith(m.lower() + ",")]
        cands = exact if len(exact) == 1 else cands
        if len(cands) != 1:
            raise QueryError((f"'{market}' matches {len(cands)} metro areas: " + "; ".join(cands[:12]) if cands
                              else f"No metro area matches '{market}'.") + " Use the exact metro area name.")
        return f"metro = '{cands[0].replace(chr(39), chr(39) * 2)}'", cands[0]

    def market_share(self, market_type, market=None, organization=None, year=None, compare_to_prior_year=False,
                     limit=10):
        """Deposit market share from the Summary of Deposits.
        With a market: organizations in that market ranked by deposits (share, rank, branches, HHI).
        With an organization (and optionally market_type 'state' + market to stay in one state): that
        organization's deposits, share and rank in every market of market_type where it has branches."""
        self._load_sod()
        year = int(year or self.sod_years[-1])
        if year not in self.sod_years:
            raise QueryError(f"Summary of Deposits years loaded: {', '.join(map(str, self.sod_years))} (as of June 30).")
        prior = year - 1 if compare_to_prior_year else None
        if prior is not None and prior not in self.sod_years:
            raise QueryError(f"No Summary of Deposits for {prior}; years loaded: {', '.join(map(str, self.sod_years))}.")
        notes = [f"Summary of Deposits, branch deposits as of June 30, {year}" + (f" vs {prior}" if prior else "") + ". "
                 "FDIC-insured institutions only (no credit unions). Deposits are booked at the branch the bank "
                 "assigns them to, so internet, credit card and custody banks can show very large deposits in their "
                 "home market."]
        if self.pro_forma:
            notes.append("Pro forma for mergers: acquired banks are counted with their acquirer in both years.")
        org_sql = None
        if organization:
            hits = [r[0] for r in self.con.execute("SELECT DISTINCT organization FROM sod WHERE year = ? AND LOWER(organization) LIKE ?",
                                                    [year, f"%{str(organization).lower()}%"]).fetchall()]
            exact = [h for h in hits if h.lower() == str(organization).lower()]
            hits = exact or hits
            if len(hits) != 1:
                raise QueryError((f"'{organization}' matches {len(hits)} organizations: " + "; ".join(hits[:12]) if hits
                                  else f"No organization matches '{organization}' in {year}."))
            org_sql = hits[0].replace("'", "''")

        if org_sql and market is None or (org_sql and market_type != "state" and market is not None):
            # one organization across markets
            col = {"state": "state", "metro": "metro", "county": "county"}.get(market_type)
            if not col:
                raise QueryError("market_type must be 'state', 'metro' or 'county'.")
            within, label = ("TRUE", "all states")
            if market is not None:
                within, label = self._resolve_market("state", market, year)
            key = f"{col} || ', ' || state" if col == "county" else col
            years = [year] + ([prior] if prior else [])
            sql = f"""WITH m AS (
                SELECT year, {key} AS market, organization, SUM(deposits_k) AS dep, COUNT(*) AS branches
                FROM sod WHERE year IN ({', '.join(map(str, years))}) AND {within} AND {col} IS NOT NULL
                GROUP BY ALL),
              t AS (SELECT year, market, SUM(dep) AS tot, COUNT(DISTINCT organization) AS orgs FROM m GROUP BY ALL),
              r AS (SELECT m.*, t.tot, t.orgs, 100.0 * m.dep / t.tot AS share,
                           RANK() OVER (PARTITION BY m.year, m.market ORDER BY m.dep DESC) AS rnk FROM m JOIN t USING (year, market))
              SELECT a.market AS "{market_type}", a.dep / 1e6 AS deposits, a.share AS deposit_share, a.rnk AS market_rank,
                     a.orgs AS organizations_in_market, a.branches, a.tot / 1e6 AS market_deposits
                     {", a.share - p.share AS change_pp, p.rnk AS prior_rank" if prior else ""}
              FROM r a {f"LEFT JOIN r p ON p.market = a.market AND p.organization = a.organization AND p.year = {prior}" if prior else ""}
              WHERE a.year = {year} AND a.organization = '{org_sql}'
              ORDER BY a.dep DESC"""
            rows = self.con.execute(sql).df().to_dict("records")
            shown = rows[:max(int(limit or 25), 1)]
            if len(rows) > len(shown):
                notes.append(f"Only the first {len(shown)} of {len(rows)} markets are shown (largest deposits first).")
            notes.append(f"{hits[0]}: deposits, share and rank in each {market_type} ({label}).")
            if prior and hits[0] in self._merged_orgs(prior, year):
                notes.append(f"{hits[0]} merged between the two dates. After a merger a bank often re-books deposits "
                             "between branches and markets, so share changes by market can reflect booking, not "
                             "customers. Say so when you report these changes; its total change is the reliable figure.")
            metrics = ["deposits", "deposit_share", "market_rank"] + (["change_pp"] if prior else [])
            return self._sod_result(shown, metrics, [market_type], notes, sql,
                                    {"organization": hits[0], "market_type": market_type, "year": year})

        if market is None:
            raise QueryError("Give a market (a state, metro area or county), or an organization to see its markets.")
        where, label = self._resolve_market(market_type, market, year)
        years = [year] + ([prior] if prior else [])
        sql = f"""WITH m AS (
            SELECT year, organization, SUM(deposits_k) AS dep, COUNT(*) AS branches
            FROM sod WHERE year IN ({', '.join(map(str, years))}) AND {where} GROUP BY ALL),
          r AS (SELECT m.*, 100.0 * dep / SUM(dep) OVER (PARTITION BY year) AS share,
                       RANK() OVER (PARTITION BY year ORDER BY dep DESC) AS rnk FROM m)
          SELECT a.organization, a.dep / 1e6 AS deposits, a.share AS deposit_share, a.rnk AS market_rank, a.branches
                 {", a.share - COALESCE(p.share, 0) AS change_pp, p.rnk AS prior_rank, 100.0 * (a.dep / p.dep - 1) AS deposit_growth_pct" if prior else ""}
          FROM r a {f"LEFT JOIN r p ON p.organization = a.organization AND p.year = {prior}" if prior else ""}
          WHERE a.year = {year} ORDER BY a.dep DESC"""
        rows = self.con.execute(sql).df().to_dict("records")
        tot, hhi, n = self._sod_totals(year, where)
        market_info = {"market": label, "market_type": market_type, "year": year, "market_deposits": round(tot / 1e6, 4),
                       "organizations": n, "branches": int(sum(r["branches"] for r in rows)), "hhi": round(hhi)}
        if prior:
            ptot, phhi, pn = self._sod_totals(prior, where)
            market_info.update({"prior_market_deposits": round(ptot / 1e6, 4), "prior_hhi": round(phhi),
                                "market_deposit_growth_pct": round(100 * (tot / ptot - 1), 4)})
        notes.append("HHI = sum of squared deposit shares (0-10,000). The 2023 DOJ/FTC Merger Guidelines treat a "
                     "market above 1,800 as highly concentrated.")
        shown = rows[:max(int(limit or 10), 1)]
        if len(rows) > len(shown):
            notes.append(f"Only the first {len(shown)} of {len(rows)} organizations are shown (largest deposits first).")
        if org_sql and not any(r["organization"] == hits[0] for r in shown):
            shown += [r for r in rows if r["organization"] == hits[0]]      # the asked-about organization, at its rank
        few = [r for r in shown if r["branches"] <= 2 and r["deposit_share"] >= 10]
        for r in shown:
            r["booked_at_few_offices"] = r in few
        if few:
            notes.append("Booked at one or two offices: " + ", ".join(r["organization"] for r in few) + ". These deposits "
                         "are mostly custody, internet or wholesale funds booked at a head office, not branch customers. "
                         "Say so, and give the leading branch-based organizations as well.")
        if prior:
            merged = self._merged_orgs(prior, year)
            hit = [r["organization"] for r in shown if r["organization"] in merged]
            for r in shown:
                r["merged_since_prior"] = r["organization"] in merged
            if hit:
                notes.append("Merged between the two dates: " + ", ".join(hit) + ". After a merger a bank often re-books "
                             "deposits between branches and markets, so its share change in one market can reflect "
                             "booking, not customers. Say so when you report these changes.")
        metrics = ["deposits", "deposit_share", "market_rank"] + (["change_pp", "deposit_growth_pct"] if prior else [])
        return self._sod_result(shown, metrics, ["organization"], notes, sql, market_info)

    def _merged_orgs(self, prior, year) -> set:
        """Organizations that absorbed a bank between June 30 of prior and June 30 of year."""
        return {r[0] for r in self.con.execute(f"""SELECT DISTINCT o.organization FROM merger_events e
            LEFT JOIN survivor s ON s.CERT = e.ACQ_CERT JOIN cert_org o ON o.CERT = COALESCE(s.SURV_CERT, e.ACQ_CERT)
            WHERE e.EFFDATE > DATE '{prior}-06-30' AND e.EFFDATE <= DATE '{year}-06-30'""").fetchall()}

    def _sod_totals(self, year, where):
        return self.con.execute(f"""SELECT SUM(dep), SUM(s * s), COUNT(*) FROM (
            SELECT dep, 100.0 * dep / SUM(dep) OVER () AS s FROM (
              SELECT organization, SUM(deposits_k) AS dep FROM sod WHERE year = {year} AND {where} GROUP BY 1))""").fetchone()

    def _sod_result(self, rows, metrics, dims, notes, sql, market):
        units = {"deposits": "$ billions", "deposit_share": "percent", "market_rank": "count", "change_pp": "points",
                 "deposit_growth_pct": "percent", "market_deposits": "$ billions"}
        for r in rows:
            for k, v in list(r.items()):
                if isinstance(v, float):
                    r[k] = None if v != v else round(v, 4)
                elif hasattr(v, "item"):
                    r[k] = v.item()
        return {"rows": rows, "market": market, "units": {m: units[m] for m in metrics},
                "notes": notes, "semantic_model_version": self.version, "sql": sql}

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
