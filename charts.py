"""Chart builders for the Ask page. Every chart is drawn from a semantic-layer result."""
from __future__ import annotations

import altair as alt
import pandas as pd

UNIT_SUFFIX = {"$ billions": "($B)", "percent": "(%)", "count": "", "index": ""}
BAR = "#5B9BD5"
BAR_LIGHT = "#B9D3EC"
SERIES = ["#4C9BE8", "#F2A65A", "#5CC08A", "#E0607E", "#A88BEB", "#E8C547", "#4DC4C4", "#B0B0B0"]  # distinct hues
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
    "GU": "Guam", "FM": "Micronesia", "AS": "American Samoa", "MP": "Northern Mariana Islands"}


def label(name: str) -> str:
    special = {"change_pct": "Change", "change_pp": "Change (points)", "change": "Change", "cet1_ratio": "CET1 ratio",
               "return_on_assets_quarterly": "Quarterly ROA", "net_interest_margin": "Net interest margin (NIM)",
               "tier1_risk_based_ratio": "Tier 1 risk-based ratio", "total_risk_based_ratio": "Total risk-based ratio",
               "noninterest_bearing_deposit_share": "Non-interest-bearing deposit share"}
    if name in special:
        return special[name]
    words = name.replace("_", " ").capitalize().split()
    acronyms = {"roa": "ROA", "roe": "ROE", "nim": "NIM", "cet1": "CET1", "fdic": "FDIC", "occ": "OCC", "cre": "CRE"}
    return " ".join(acronyms.get(w.lower(), w) for w in words)


def value_format(unit: str) -> tuple[str, str]:
    """(d3 number format, suffix) for value labels."""
    if unit == "percent":
        return ".2f", "%"
    if unit == "$ billions":
        return ",.1f", "B"
    return ",.0f", ""


def build(chart: dict, result: dict, max_bars: int = 20):
    """Return (altair chart, note or None)."""
    df = pd.DataFrame(result["rows"])
    x, y, color = chart["x"], chart["y"], chart.get("color")
    series_note = None
    if chart.get("only_series") and color in df.columns:
        df = df[df[color].isin(chart["only_series"])].reset_index(drop=True)
        series_note = f"Showing the {len(chart['only_series'])} largest series. The full table is under How I got this."
    if chart.get("only") and x in df.columns:
        df = df[df[x].astype(str).isin([str(v) for v in chart["only"]])].reset_index(drop=True)
    conc = result.get("concentration") or {}
    if chart["kind"] == "bar" and not color and conc.get("metric") == y and conc.get("group_rate_without_driver") is not None \
            and conc.get("group", "").split(" = ")[0] == x:
        grp = conc["group"].split(" = ", 1)[1]
        if grp in set(df[x].astype(str)):
            extra = {x: f"{grp} excl. {conc['driver_bank']}", y: conc["group_rate_without_driver"], "_excl": True}
            df = pd.concat([df, pd.DataFrame([extra])], ignore_index=True)
    if "_excl" not in df.columns:
        df["_excl"] = False
    df["_excl"] = df["_excl"].fillna(False).astype(bool)
    if x == "state":
        def nice(v):
            code, sep, rest = str(v).partition(" excl. ")
            return STATE_NAMES.get(code, code) + sep + rest
        df[x] = df[x].map(nice)
    if chart["kind"] == "bar" and color == "period":     # segments over time read better as lines
        chart = {**chart, "kind": "line"}
        x, color = "period", chart["x"]
    unit = result.get("units", {}).get(y, "")
    y_title = f"{label(y)} {UNIT_SUFFIX.get(unit, '')}".strip()
    fmt, suffix = value_format(unit)
    prefix = "$" if unit == "$ billions" else ""
    df["_label"] = df[y].map(lambda v: "" if pd.isna(v) else f"{prefix}{v:{fmt}}{suffix}")
    note = series_note

    tooltip = [alt.Tooltip(f"{x}:N", title=label(x)), alt.Tooltip(f"{y}:Q", title=y_title, format=",.2f")]
    if color:
        tooltip.insert(1, alt.Tooltip(f"{color}:N", title=label(color)))

    if chart["kind"] == "line" and color and not chart.get("only") and df[color].nunique() > 2 \
            and (df[color].astype(str) == "Unknown").any():
        df = df[df[color].astype(str) != "Unknown"].reset_index(drop=True)
        note = "Unknown (banks with no institution record) is left out of the chart; it is in the table under How I got this."
    if chart["kind"] == "line":
        df = df[df[y].notna()].reset_index(drop=True)          # no empty quarters on the axis (e.g. 2024Q3)
        enc = dict(x=alt.X(f"{x}:O", title=None, sort=None, axis=alt.Axis(labelAngle=0)),
                   y=alt.Y(f"{y}:Q", title=y_title, scale=alt.Scale(zero=False), axis=alt.Axis(gridOpacity=0.3)),
                   tooltip=tooltip)
        if color:
            enc["color"] = alt.Color(f"{color}:N", title=label(color), legend=alt.Legend(orient="bottom", columns=4,
                                     labelLimit=260), scale=alt.Scale(range=SERIES))
        base = alt.Chart(df).encode(**enc)
        solid = {} if color else {"color": BAR}
        lines = base.mark_line(strokeWidth=2.5, **solid)
        points = base.mark_circle(size=55, **solid)
        layers = [lines, points]
        if "merger_quarter" in df.columns and df["merger_quarter"].fillna(False).astype(bool).any():
            mq = df[df["merger_quarter"].fillna(False).astype(bool)]
            layers.append(alt.Chart(mq).mark_point(shape="diamond", size=260, filled=False, color="#8A94A6",
                                                     strokeWidth=2).encode(x=alt.X(f"{x}:O", sort=None), y=f"{y}:Q"))
            note = (note + " " if note else "") + ("Gray diamonds: merger quarters. Call-report income in these "
                                                   "quarters can differ materially from reported results.")
        if not color:   # label the last point
            last = df.iloc[[-1]]
            layers.append(alt.Chart(last).mark_text(align="left", dx=8, fontSize=12, fontWeight="bold", color=BAR)
                          .encode(x=alt.X(f"{x}:O", sort=None), y=f"{y}:Q", text="_label:N"))
        return alt.layer(*layers).properties(title=chart.get("title", ""), height=320), note

    # bar
    if not chart.get("only") and len(df) > 2 and (df[x].astype(str) == "Unknown").any():
        df = df[df[x].astype(str) != "Unknown"].reset_index(drop=True)
        note = "Unknown (banks with no institution record) is left out of the chart; it is in the table under How I got this."
    if not color and (df[y] < 0).any():
        max_bars = 10 ** 6
    if not color and len(df[~df["_excl"]]) > max_bars:
        note = f"Showing the top {max_bars} of {len(df[~df['_excl']])}. The full table is under How I got this."
        df = pd.concat([df[~df["_excl"]].nlargest(max_bars, y), df[df["_excl"]]], ignore_index=True)
    order = df[~df["_excl"]].groupby(x)[y].max().sort_values(ascending=False).index.tolist()
    for _, r in df[df["_excl"]].iterrows():           # the "excl." bar sits right under its group
        base_name = str(r[x]).split(" excl. ")[0]
        order.insert(order.index(base_name) + 1 if base_name in order else len(order), r[x])
        note = (f"Light bar: {base_name} without {str(r[x]).split(' excl. ')[1]}, which holds most of its "
                "problem loans. Computed in code from the same governed data.")
    y_axis = alt.Y(f"{x}:N", title=None, sort=order,
                   axis=alt.Axis(labelLimit=320, labelPadding=8, labelFontSize=12, ticks=False, domain=False))
    x_axis = alt.X(f"{y}:Q", title=y_title, axis=alt.Axis(gridOpacity=0.25, tickCount=5),
                   scale=alt.Scale(zero=True))
    if color:
        bars = alt.Chart(df).mark_bar(cornerRadiusEnd=3).encode(
            y=y_axis, x=x_axis, yOffset=alt.YOffset(f"{color}:N"),
            color=alt.Color(f"{color}:N", title=label(color), legend=alt.Legend(orient="bottom")), tooltip=tooltip)
        height = df[x].nunique() * (16 * df[color].nunique() + 14)
        return bars.properties(title=chart.get("title", ""), height=min(max(height, 160), 640)), note
    base = alt.Chart(df).encode(y=y_axis, x=x_axis, tooltip=tooltip)
    bars = base.mark_bar(cornerRadiusEnd=3, height={"band": 0.7}).encode(
        color=alt.condition("datum._excl", alt.value(BAR_LIGHT), alt.value(BAR)))
    if (df[y] < 0).any():                    # gainers and losers: bars on each side of zero, labels outside
        n_all = len(df)
        if n_all > 10:                       # the 5 largest gains and the 5 largest declines
            df = pd.concat([df[df[y] >= 0].nlargest(5, y), df[df[y] < 0].nsmallest(5, y)], ignore_index=True)
            note = (f"Showing the 5 largest gains and the 5 largest declines of {n_all}. "
                    "The full list is under How I got this.")
        order = df.sort_values(y, ascending=False)[x].tolist()
        y_axis = alt.Y(f"{x}:N", title=None, sort=order,
                       axis=alt.Axis(labelLimit=320, labelPadding=8, labelFontSize=12, ticks=False, domain=False))
        df["_sign"] = df[y].map(lambda v: "Gain" if v >= 0 else "Loss")
        df["_label"] = df[y].map(lambda v: "" if pd.isna(v) else f"{'+' if v >= 0 else '−'}{prefix}{abs(v):{fmt}}{suffix}")
        base = alt.Chart(df).encode(y=y_axis, x=x_axis, tooltip=tooltip)
        bars = base.mark_bar(cornerRadiusEnd=3, height={"band": 0.7}).encode(
            color=alt.Color("_sign:N", scale=alt.Scale(domain=["Gain", "Loss"], range=[BAR, "#E07B6A"]), legend=None))
        pos = base.transform_filter("datum._sign == 'Gain'").mark_text(
            align="left", dx=6, fontSize=12, fontWeight="bold", color="#8A94A6").encode(text="_label:N")
        neg = base.transform_filter("datum._sign == 'Loss'").mark_text(
            align="right", dx=-6, fontSize=12, fontWeight="bold", color="#8A94A6").encode(text="_label:N")
        return (bars + pos + neg).properties(title=chart.get("title", ""), height=alt.Step(30)), note
    df["_inside"] = df[y].abs() >= 0.15 * df[y].abs().max()        # short bars get their label outside
    base = alt.Chart(df).encode(y=y_axis, x=x_axis, tooltip=tooltip)
    bench = (result.get("benchmark") or {}).get(y)
    inside = base.transform_filter("datum._inside").mark_text(
        align="right", dx=-8, fontSize=12, fontWeight="bold").encode(
        text="_label:N", color=alt.condition("datum._excl", alt.value("#1F3B57"), alt.value("white")))
    outside = base.transform_filter("!datum._inside").mark_text(
        align="left", dx=6, fontSize=12, fontWeight="bold", color="#8A94A6").encode(text="_label:N")
    layers = bars + inside + outside
    if bench is not None:                    # the whole group's aggregate, as a reference line
        line = pd.DataFrame([{"v": bench, "t": f"All in group: {prefix}{bench:{fmt}}{suffix}"}])
        rule = alt.Chart(line).mark_rule(strokeDash=[5, 4], color="#F2A65A", strokeWidth=2).encode(x="v:Q")
        txt = alt.Chart(line).mark_text(align="left", dx=5, dy=-8, color="#F2A65A", fontWeight="bold",
                                        fontSize=12).encode(x="v:Q", y=alt.value(0), text="t:N")
        layers = layers + rule + txt
        note = (note + " " if note else "") + "Dashed line: the aggregate for the whole group in the question."
    return layers.properties(title=chart.get("title", ""), height=alt.Step(38)), note


def scorecard(result: dict, request: dict, lower_is_better: set, higher_is_better: set):
    """A pandas Styler: one row per group, one column per metric, best value per column highlighted.
    Returns None unless the result is a small comparison of 2+ metrics across one dimension."""
    rows = result.get("rows") or []
    dims = [d for d in request.get("dimensions") or [] if rows and d in rows[0]]
    metrics = [m for m in request.get("metrics") or [] if rows and m in rows[0]]
    if len(dims) != 1 or dims[0] == "period" or len(metrics) < 2 or not 2 <= len(rows) <= 15:
        return None
    pct = sum(result.get("units", {}).get(m) == "percent" for m in metrics)
    if request.get("order_by") and pct < 2:           # one ratio plus context columns: a ranked list, not a scorecard
        return None
    df = pd.DataFrame(rows)[[dims[0]] + metrics]
    # Same rule as rankings: Unknown (and territories, by state) are not peers, so they never get "best"
    non_peer = {"Unknown"} | ({"PR", "VI", "GU", "FM", "AS", "MP", "MH", "PW"} if dims[0] == "state" else set())
    keep = ~df[dims[0]].astype(str).isin(non_peer)
    if keep.sum() >= 2:
        df = df[keep]
    units = result.get("units", {})
    fmt = {m: ("{:,.2f}%" if units.get(m) == "percent" else "${:,.1f}B" if units.get(m) == "$ billions" else "{:,.0f}")
           for m in metrics}

    names = {c: label(c) for c in df.columns}
    directional = {names[m]: ("min" if m in lower_is_better else "max")
                   for m in metrics if m in lower_is_better or m in higher_is_better}

    def best(col):
        how = directional.get(col.name)
        if how is None or col.isna().all():
            return [""] * len(col)
        target = col.min() if how == "min" else col.max()
        return ["font-weight: 700; background-color: rgba(91,155,213,0.28)" if v == target else "" for v in col]

    df = df.sort_values(metrics[0], ascending=metrics[0] in lower_is_better).rename(columns=names)
    return df.style.format({names[m]: f for m, f in fmt.items()}, na_rep="—").apply(best).hide(axis="index")
