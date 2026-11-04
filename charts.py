"""Chart builders for the Ask page. Every chart is drawn from a semantic-layer result."""
from __future__ import annotations

import altair as alt
import pandas as pd

UNIT_SUFFIX = {"$ billions": "($B)", "percent": "(%)", "count": ""}
BAR = "#5B9BD5"


def label(name: str) -> str:
    return name.replace("_", " ").capitalize()


def value_format(unit: str) -> tuple[str, str]:
    """(d3 number format, suffix) for value labels."""
    if unit == "percent":
        return ".2f", "%"
    if unit == "$ billions":
        return ",.1f", "B"
    return ",.0f", ""


def build(chart: dict, result: dict, max_bars: int = 15):
    """Return (altair chart, note or None)."""
    df = pd.DataFrame(result["rows"])
    x, y, color = chart["x"], chart["y"], chart.get("color")
    if chart.get("only") and x in df.columns:
        df = df[df[x].astype(str).isin([str(v) for v in chart["only"]])].reset_index(drop=True)
    if chart["kind"] == "bar" and color == "period":     # segments over time read better as lines
        chart = {**chart, "kind": "line"}
        x, color = "period", chart["x"]
    unit = result.get("units", {}).get(y, "")
    y_title = f"{label(y)} {UNIT_SUFFIX.get(unit, '')}".strip()
    fmt, suffix = value_format(unit)
    prefix = "$" if unit == "$ billions" else ""
    df["_label"] = df[y].map(lambda v: "" if pd.isna(v) else f"{prefix}{v:{fmt}}{suffix}")
    note = None

    tooltip = [alt.Tooltip(f"{x}:N", title=label(x)), alt.Tooltip(f"{y}:Q", title=y_title, format=",.2f")]
    if color:
        tooltip.insert(1, alt.Tooltip(f"{color}:N", title=label(color)))

    if chart["kind"] == "line":
        enc = dict(x=alt.X(f"{x}:O", title=None, sort=None, axis=alt.Axis(labelAngle=0)),
                   y=alt.Y(f"{y}:Q", title=y_title, scale=alt.Scale(zero=False), axis=alt.Axis(gridOpacity=0.3)),
                   tooltip=tooltip)
        if color:
            enc["color"] = alt.Color(f"{color}:N", title=label(color), legend=alt.Legend(orient="bottom", columns=4))
        base = alt.Chart(df).encode(**enc)
        solid = {} if color else {"color": BAR}
        lines = base.mark_line(strokeWidth=2.5, **solid)
        points = base.mark_circle(size=55, **solid)
        layers = [lines, points]
        if not color:   # label the last point
            last = df.iloc[[-1]]
            layers.append(alt.Chart(last).mark_text(align="left", dx=8, fontSize=12, fontWeight="bold", color=BAR)
                          .encode(x=alt.X(f"{x}:O", sort=None), y=f"{y}:Q", text="_label:N"))
        return alt.layer(*layers).properties(title=chart.get("title", ""), height=320), note

    # bar
    if not color and len(df) > max_bars:
        note = f"Showing the top {max_bars} of {len(df)}. The full table is under How I got this."
        df = df.nlargest(max_bars, y)
    order = df.groupby(x)[y].max().sort_values(ascending=False).index.tolist()
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
    bars = base.mark_bar(cornerRadiusEnd=3, color=BAR, height={"band": 0.7})
    text = base.mark_text(align="right", dx=-8, fontSize=12, fontWeight="bold", color="white").encode(text="_label:N")
    return (bars + text).properties(title=chart.get("title", ""), height=alt.Step(38)), note


def scorecard(result: dict, request: dict, lower_is_better: set, higher_is_better: set):
    """A pandas Styler: one row per group, one column per metric, best value per column highlighted.
    Returns None unless the result is a small comparison of 2+ metrics across one dimension."""
    rows = result.get("rows") or []
    dims = [d for d in request.get("dimensions") or [] if rows and d in rows[0]]
    metrics = [m for m in request.get("metrics") or [] if rows and m in rows[0]]
    if len(dims) != 1 or dims[0] == "period" or len(metrics) < 2 or not 2 <= len(rows) <= 15:
        return None
    df = pd.DataFrame(rows)[[dims[0]] + metrics]
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
