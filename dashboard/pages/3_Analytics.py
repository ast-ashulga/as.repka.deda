"""Analytics: star growth, velocity and tech-stack changes for one domain. Read-only.

Metric values come from repka.dashboard_data (which uses store.metrics). No SQL lives here.
Colors follow the dataviz reference palette (categorical slots fixed by rank, light and dark steps).
"""

import sqlite3
from contextlib import closing

import altair as alt
import streamlit as st

from repka import dashboard_data as data
from repka.config import ConfigError, db_path
from repka.domain.schema import DEFAULT_SCAN_EVERY, load_domains
from repka.store.db import connect
from repka.textutil import printable

SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
              "grid": "#e1e0d9", "base": "#c3c2b7", "series": SERIES_LIGHT},
    "dark": {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
             "grid": "#2c2c2a", "base": "#383835", "series": SERIES_DARK},
}
WINDOWS = (7, 30, 90)
MAX_SERIES = 8  # the categorical palette has eight slots; charts never cycle colors


def _theme() -> dict:
    try:
        return THEMES["dark" if st.context.theme.type == "dark" else "light"]
    except AttributeError:
        return THEMES["light"]


def _line_chart(df, x, y, color, title, domain, colors, theme, zero=True):
    df = df.assign(**{x: df[x].dt.tz_localize(None)})  # Vega-lite wants naive UTC timestamps
    chart = (
        alt.Chart(df)
        .mark_line(point=True, strokeWidth=2)
        .encode(
            x=alt.X(f"{x}:T", title=None),
            y=alt.Y(f"{y}:Q", title=title, scale=alt.Scale(zero=zero)),
            color=alt.Color(
                f"{color}:N",
                scale=alt.Scale(domain=domain, range=colors),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            tooltip=[
                alt.Tooltip(f"{color}:N", title="series"),
                alt.Tooltip(f"{x}:T", title="taken"),
                alt.Tooltip(f"{y}:Q", title=title),
            ],
        )
        .properties(height=260)
    )
    return (
        chart.configure(background=theme["surface"])
        .configure_axis(labelColor=theme["muted"], titleColor=theme["ink2"], gridColor=theme["grid"],
                        domainColor=theme["base"])
        .configure_legend(labelColor=theme["ink2"])
    )


def _signed(value) -> str:
    return "n/a" if value is None or value != value else f"{int(value):+d}"


st.set_page_config(page_title="repka analytics", layout="wide")
st.title("Analytics")
st.caption("Read-only. Data changes only when `repka run` writes snapshots.")

if not db_path().exists():
    st.info("No database yet. Run `repka db migrate`, then `repka run`.")
    st.stop()

try:
    domains = load_domains()
except ConfigError as exc:
    st.error(f"Domain files are invalid: {exc}")
    st.stop()
if not domains:
    st.info("No domains yet. Add one with `repka domain new`.")
    st.stop()

names = {d.id: printable(d.name) for d in domains}
controls = st.columns([2, 1, 1])
domain_id = controls[0].selectbox("Domain", list(names), format_func=names.get)
days = controls[1].selectbox("Window", WINDOWS, format_func=lambda d: f"last {d} days")
top = controls[2].number_input("Top", min_value=1, max_value=50, value=10)
theme = _theme()

try:
    with closing(connect(readonly=True)) as conn:
        repos = data.repos_frame(conn, domain_id, accepted_only=True)
        if repos.empty:
            st.info(f"No accepted repos in {names[domain_id]} yet. Run `repka run` first.")
            st.stop()
        history = data.metrics_history(conn, repos["repo_id"])
        if history.empty:
            st.info("No snapshots yet. Run `repka run` first.")
            st.stop()

        # (a) stars and 7-day star delta over time, for the top repos by stars (colors follow that rank)
        st.subheader("Stars over time")
        st.caption("Lines break where a scan was missed (snapshots further apart than the domain's scan interval). "
                   "Delta 7d is n/a until two snapshots exist.")
        gap = data.gap_after(next((d.scan_every for d in domains if d.id == domain_id), DEFAULT_SCAN_EVERY))
        top_repos = repos.head(MAX_SERIES)
        options = top_repos["full_name"].tolist()
        picked = st.multiselect("Repos", options, default=options[:3], max_selections=MAX_SERIES)
        picked_ids = top_repos.loc[top_repos["full_name"].isin(picked), "repo_id"]
        series_colors = {name: theme["series"][i] for i, name in enumerate(options) if name in picked}
        if picked:
            series = data.with_gaps(history[history["repo_id"].isin(picked_ids)], "repo_id", "taken_at", "stars", gap)
            left, right = st.columns(2)
            left.altair_chart(
                _line_chart(series, "taken_at", "stars", "full_name", "stars", list(series_colors),
                            list(series_colors.values()), theme, zero=False),
            )
            picked_history = history[history["repo_id"].isin(picked_ids)]
            if picked_history["stars_delta_7d"].notna().any():
                delta_series = data.with_gaps(picked_history, "repo_id", "taken_at", "stars_delta_7d", gap)
                right.altair_chart(
                    _line_chart(delta_series, "taken_at", "stars_delta_7d", "full_name", "stars gained, 7d",
                                list(series_colors), list(series_colors.values()), theme),
                )
            else:  # no delta yet: an empty chart would have no scale
                right.info("Not enough snapshots yet: deltas appear after the second scan.")

        # (b) velocity ranking with a per-repo trend
        st.subheader("Velocity")
        st.caption("Velocity = activity score from commits, active weeks and releases (see store.metrics).")
        ranking = data.velocity_table(repos, history).head(top)
        st.dataframe(
            ranking.assign(velocity=ranking["velocity"].map(lambda v: "n/a" if v != v or v is None else f"{v:.2f}"))
            .rename(columns={"full_name": "repo", "velocity": "velocity now"})
            .drop(columns=["repo_id"]),
            column_config={"trend": st.column_config.LineChartColumn("velocity trend")},
            hide_index=True,
        )

        # (c) top movers over the chosen window
        st.subheader("Top movers")
        st.caption(f"Stars gained in the last {days} days. n/a: no snapshot old enough to compare with.")
        movers = data.window_deltas(conn, repos["repo_id"], days).head(top)
        st.dataframe(
            movers.assign(stars_delta=movers["stars_delta"].map(_signed))
            .rename(columns={"full_name": "repo", "stars_delta": f"stars, {days}d"})
            .drop(columns=["repo_id"]),
            hide_index=True,
        )

        # (d) tech stack: dependency changes per repo, and dependency popularity in the domain
        st.subheader("Stack")
        st.caption("Dependencies added or removed per key-file version. Popularity counts repos in this domain.")
        repo_name = st.selectbox("Repo", repos["full_name"].tolist(), key="stack_repo")
        stack = data.stack_history(conn, int(repos.loc[repos["full_name"] == repo_name, "repo_id"].iloc[0]))
        if stack.empty:
            st.info("No dependency changes recorded for this repo yet.")
        else:
            st.dataframe(stack.drop(columns=["blob_oid"]), hide_index=True)
        popularity = data.dependency_popularity(conn, repos["repo_id"])
        if popularity.empty:
            st.info("No dependency data for this domain yet. It fills in after key files are fetched.")
        else:
            order = (
                popularity[popularity["taken_at"] == popularity["taken_at"].max()]
                .sort_values("repos", ascending=False, kind="stable")["dependency"].tolist()
            )
            st.altair_chart(
                _line_chart(popularity, "taken_at", "repos", "dependency", "repos using it", order,
                            theme["series"][: len(order)], theme),
            )
except sqlite3.Error as exc:
    st.error(f"Could not read the database (run `repka db migrate`?): {exc}")
