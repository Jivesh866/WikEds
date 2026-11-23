import inspect
import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from aggregator import HISTORY_DAYS, LOCAL_TZ, WINDOW_HOURS, Aggregator

DB_DEFAULT = os.environ.get("WIKI_DB", "wiki.db")
STALL_SECONDS = 120   # warn if nothing has been ingested for this long

st.set_page_config(page_title="Wikipedia live edits", layout="wide")


LIGHT = dict(surface="#fcfcfb", text="#0b0b0b", text2="#52514e", muted="#898781",
             grid="#e1e0d9", axis="#c3c2b7", human="#2a78d6", bot="#eb6834")
DARK = dict(surface="#1a1a19", text="#f0efec", text2="#c3c2b7", muted="#898781",
            grid="#2c2c2a", axis="#383835", human="#3987e5", bot="#d95926")
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

_PLOT_WIDTH = (
    {"width": "stretch"} if "width" in inspect.signature(st.plotly_chart).parameters
    else {"use_container_width": True}
)
_TABLE_WIDTH = (
    {"width": "stretch"} if "width" in inspect.signature(st.dataframe).parameters
    else {"use_container_width": True}
)


def palette():
    """Pick light/dark chart colors to match the Streamlit theme."""
    try:
        theme = st.context.theme.type
    except Exception:
        theme = None
    if theme is None:
        theme = st.get_option("theme.base")
    return DARK if theme == "dark" else LIGHT


def style(fig, c, height, legend=False, ytitle=None, horizontal=False, right=8, hover=None):
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=right, t=40 if legend else 8, b=8),
        paper_bgcolor=c["surface"], plot_bgcolor=c["surface"],
        font=dict(family=FONT, size=12, color=c["text2"]),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0,
                    bgcolor="rgba(0,0,0,0)", font=dict(color=c["text2"])),
        hoverlabel=dict(bgcolor=c["surface"], bordercolor=c["axis"],
                        font=dict(family=FONT, color=c["text"])),
        hovermode=hover or ("y" if horizontal else "x unified"),
        bargap=0.3,
    )
    grid = dict(showgrid=True, gridcolor=c["grid"], gridwidth=1, zeroline=False, showline=False)
    base = dict(showgrid=False, showline=True, linecolor=c["axis"], zeroline=False)
    ticks = dict(tickfont=dict(color=c["muted"], size=11), ticks="", automargin=True)
    if horizontal:
        fig.update_xaxes(**grid, **ticks, tickformat=",d")
        fig.update_yaxes(**base, **ticks)
    else:
        fig.update_xaxes(**base, **ticks)
        fig.update_yaxes(**grid, **ticks, tickformat=",d", rangemode="tozero")
    if ytitle:
        fig.update_yaxes(title_text=ytitle, title_font=dict(color=c["muted"], size=11))
    return fig


def bar(c, who, **kw):
    """One bar trace in the human/bot color with a 2px surface gap between segments."""
    color = c["human"] if who == "Humans" else c["bot"]
    return go.Bar(name=who, marker=dict(color=color, line=dict(color=c["surface"], width=2)), **kw)


def show_plot(fig, key):
    st.plotly_chart(fig, theme=None, key=key, config={"displayModeBar": False}, **_PLOT_WIDTH)


def show_table(df, label="Table view", **kw):
    with st.expander(label):
        st.dataframe(df, hide_index=True, **_TABLE_WIDTH, **kw)


def naive(series):
    return series.dt.tz_localize(None)


def fmt_age(seconds):
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60} min"


# ----------------------------------------------------------------- data access
@st.cache_resource(show_spinner="Loading recent events...")
def get_aggregator(db_path):
    return Aggregator(db_path, window_hours=WINDOW_HOURS, history_days=HISTORY_DAYS)


@st.cache_data(ttl=600, show_spinner=False)
def wiki_options(db_path):
    return get_aggregator(db_path).available_wikis(minutes=WINDOW_HOURS * 60, limit=80)


# ----------------------------------------------------------------- charts
def rate_chart(c, rate, include_bots, minutes):
    x = naive(rate["minute"])
    fig = go.Figure()
    fig.add_trace(bar(c, "Humans", x=x, y=rate["humans"], hovertemplate="%{y:,}"))
    if include_bots:
        fig.add_trace(bar(c, "Bots", x=x, y=rate["bots"], hovertemplate="%{y:,}"))
    fig.add_trace(go.Scatter(
        name="5-min average", x=x, y=rate["edits_avg"], mode="lines",
        line=dict(color=c["text"], width=2), hovertemplate="%{y:,.1f}"))
    fig.update_layout(barmode="stack")
    fig.update_xaxes(tickformat="%H:%M")
    return style(fig, c, 330, legend=True, ytitle="edits per minute")


def stacked_hbar(c, df, label, include_bots, key_prefix, height=330, unit="edits"):
    """Horizontal stacked humans/bots bars, largest at the top."""
    d = df.copy()
    d["humans"] = d["edits"] - d["bot_edits"]
    fig = go.Figure()
    fig.add_trace(bar(c, "Humans", y=d[label], x=d["humans"], orientation="h",
                      hovertemplate="%{y}: %{x:,} " + unit + "<extra>Humans</extra>"))
    if include_bots:
        fig.add_trace(bar(c, "Bots", y=d[label], x=d["bot_edits"], orientation="h",
                          hovertemplate="%{y}: %{x:,} " + unit + "<extra>Bots</extra>"))
    fig.update_layout(barmode="stack")
    fig.update_yaxes(autorange="reversed")
    return style(fig, c, height, legend=include_bots, horizontal=True)


# ----------------------------------------------------------------- Live tab
def live_panel(db_path, minutes, include_bots, wikis):
    agg = get_aggregator(db_path)
    agg.refresh()
    c = palette()
    health = agg.health()

    age = health["newest_ingest_age_s"]
    if age is None:
        st.info("No events yet. Start `listener.py` and give it a few seconds.")
        return
    if age > STALL_SECONDS:
        st.warning(f"The listener looks stalled: the newest event was ingested {fmt_age(age)} ago.")
    else:
        st.caption(f"Live: newest event ingested {fmt_age(age)} ago · "
                   f"{health['rows_cached']:,} events in memory · times in {LOCAL_TZ}")

    h = agg.headline(minutes, include_bots, wikis)
    k = st.columns(5)
    k[0].metric("Edits per minute", f"{h['edits_per_min']:,.0f}")
    k[1].metric(f"Edits, last {h['minutes']} min", f"{h['edits']:,}")
    k[2].metric("Bot share", f"{h['bot_pct']:.1f}%" if include_bots else "excluded")
    k[3].metric("Active wikis", f"{h['active_wikis']:,}")
    k[4].metric("Editors", f"{h['editors']:,}")

    st.subheader("Edits per minute")
    rate = agg.edits_per_minute(minutes, include_bots, wikis)
    show_plot(rate_chart(c, rate, include_bots, minutes), "live_rate")
    st.caption("The current minute is left out until it is complete.")
    show_table(rate.assign(minute=rate["minute"].dt.strftime("%H:%M")), "Table view: edits per minute")

    left, mid, right = st.columns(3)
    with left:
        st.subheader("Busiest wikis")
        tw = agg.top_wikis(minutes, 10, include_bots)
        show_plot(stacked_hbar(c, tw, "wiki", include_bots, "wikis"), "live_wikis")
        show_table(tw, "Table view: busiest wikis")
    with mid:
        st.subheader("Wikipedia languages")
        bl = agg.by_language(minutes, 10, include_bots)
        show_plot(stacked_hbar(c, bl, "language", include_bots, "langs"), "live_langs")
        show_table(bl, "Table view: languages")
    with right:
        st.subheader("Event types")
        st.dataframe(agg.by_type(minutes, include_bots, wikis), hide_index=True, **_TABLE_WIDTH)
        st.caption("edit and new are page changes; log covers moves, deletions, blocks and so on.")

    a, b = st.columns(2)
    with a:
        st.subheader("Hottest pages")
        tp = agg.top_pages(minutes, 10, include_bots, wikis)
        tp["last_edit"] = tp["last_edit"].dt.strftime("%H:%M:%S")
        st.dataframe(tp, hide_index=True, **_TABLE_WIDTH)
        st.caption("Most edited pages in the window" + ("." if include_bots else ", humans only."))
    with b:
        st.subheader("Biggest changes (last hour)")
        be = agg.biggest_edits(60, 10, include_bots, wikis)
        be["edit_time"] = be["edit_time"].dt.strftime("%H:%M:%S")
        st.dataframe(be, hide_index=True, **_TABLE_WIDTH)

    with st.expander("Latest events"):
        ev = agg.recent_events(25, include_bots, wikis)
        ev["edit_time"] = ev["edit_time"].dt.strftime("%H:%M:%S")
        st.dataframe(ev, hide_index=True, **_TABLE_WIDTH)


# ----------------------------------------------------------------- History tab
def history_panel(db_path, include_bots, wikis):
    agg = get_aggregator(db_path)
    agg.refresh_history()
    c = palette()
    cov = agg.history_coverage()

    if cov["hours_with_data"] == 0:
        st.info("No history yet. The listener rolls raw events up into hourly summaries every "
                "10 minutes, so this fills in shortly after the first full hour. To fill it "
                "from existing data right away, run `python rollup.py`.")
        return

    choices = [d for d in (1, 7, 14, 30) if d <= HISTORY_DAYS] or [HISTORY_DAYS]
    days = st.radio("Range", choices, index=min(1, len(choices) - 1), horizontal=True,
                    format_func=lambda d: "Last 24 hours" if d == 1 else f"Last {d} days")
    st.caption(
        f"Summary covers {cov['first_hour']:%d %b %Y %H:%M} to {cov['last_hour']:%d %b %Y %H:%M} "
        f"({LOCAL_TZ}): {cov['hours_with_data']:,} of {cov['hours_in_span']:,} hours have data. "
        "Breaks in a line are hours when the listener was not running."
    )

    # hourly lines (gaps stay gaps)
    st.subheader("Edits per hour")
    hr = agg.history_hourly(days, include_bots, wikis)
    x = naive(hr["hour"])
    fig = go.Figure()
    fig.add_trace(go.Scatter(name="Humans", x=x, y=hr["humans"], mode="lines", connectgaps=False,
                             line=dict(color=c["human"], width=2), hovertemplate="%{y:,.0f}"))
    if include_bots:
        fig.add_trace(go.Scatter(name="Bots", x=x, y=hr["bots"], mode="lines", connectgaps=False,
                                 line=dict(color=c["bot"], width=2), hovertemplate="%{y:,.0f}"))
    fig.update_xaxes(tickformat="%d %b %H:%M")
    show_plot(style(fig, c, 320, legend=True, ytitle="edits per hour"), "hist_hourly")
    show_table(hr.assign(hour=hr["hour"].dt.strftime("%d %b %H:%M")), "Table view: edits per hour")

    left, right = st.columns(2)
    with left:
        st.subheader("Edits per day")
        dd = agg.history_daily(days if days > 1 else 7, include_bots, wikis)
        dx = dd["day"]
        fig = go.Figure()
        cd = dd["hours_covered"]
        fig.add_trace(bar(c, "Humans", x=dx, y=dd["humans"], customdata=cd,
                          hovertemplate="%{y:,.0f} (hours with data: %{customdata})"))
        if include_bots:
            fig.add_trace(bar(c, "Bots", x=dx, y=dd["bots"], customdata=cd,
                              hovertemplate="%{y:,.0f} (hours with data: %{customdata})"))
        fig.update_layout(barmode="stack")
        fig.update_xaxes(tickformat="%d %b")
        show_plot(style(fig, c, 320, legend=True, ytitle="edits per day"), "hist_daily")
        st.caption("Today is a partial day. Hover a bar to see how many hours it covers.")
    with right:
        st.subheader("Typical day")
        hod = agg.history_hour_of_day(days, include_bots, wikis)
        hod["avg_humans"] = hod["avg_edits"] - hod["avg_bots"]
        labels = hod["hour_of_day"].map(lambda h: f"{h:02d}")
        fig = go.Figure()
        fig.add_trace(bar(c, "Humans", x=labels, y=hod["avg_humans"],
                          hovertemplate="%{y:,.0f} per hour"))
        if include_bots:
            fig.add_trace(bar(c, "Bots", x=labels, y=hod["avg_bots"],
                              hovertemplate="%{y:,.0f} per hour"))
        fig.update_layout(barmode="stack")
        show_plot(style(fig, c, 320, legend=True, ytitle=f"average edits per hour ({LOCAL_TZ})"),
                  "hist_hod")
        st.caption("Average over the selected range for each hour of the day.")

    daily = dd.assign(day=dd["day"].dt.strftime("%a %d %b")).rename(
        columns={"hours_covered": "hours with data", "net_bytes": "net bytes"})
    show_table(daily, "Table view: edits per day")

    st.subheader("Busiest wikis")
    tw = agg.history_top_wikis(days, 10, include_bots)
    show_plot(stacked_hbar(c, tw, "wiki", include_bots, "hwikis", height=340), "hist_wikis")
    show_table(tw, "Table view: busiest wikis")
    if wikis:
        st.caption("The wiki filter in the sidebar applies to the charts above, not to this ranking.")


# ----------------------------------------------------------------- page
st.title("Wikipedia live edits")

st.sidebar.header("Controls")
db_path = st.sidebar.text_input("Database file", DB_DEFAULT)
try:
    agg = get_aggregator(db_path)
except FileNotFoundError as e:
    st.error(str(e))
    st.stop()

agg.refresh()
minutes = st.sidebar.select_slider(
    "Live window", options=[5, 15, 30, 60, 180], value=15, format_func=lambda m: f"{m} min")
include_bots = st.sidebar.checkbox("Include bots", value=True)
wikis = st.sidebar.multiselect("Wikis (empty = all)", wiki_options(db_path))
refresh_label = st.sidebar.selectbox("Auto-refresh", ["Off", "5 s", "10 s", "30 s"], index=2)
interval = {"Off": None, "5 s": 5, "10 s": 10, "30 s": 30}[refresh_label]
st.sidebar.caption(f"Live data: last {WINDOW_HOURS} hours of raw events. "
                   f"History: hourly summaries, last {HISTORY_DAYS} days.")

tab_live, tab_hist = st.tabs(["Live", "History"])
with tab_live:
    # a fragment re-runs by itself every `interval` seconds without redrawing the whole page
    st.fragment(live_panel, run_every=interval)(db_path, minutes, include_bots, wikis)
with tab_hist:
    history_panel(db_path, include_bots, wikis) 