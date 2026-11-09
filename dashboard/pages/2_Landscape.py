"""Landscape: a 2-D map of a domain's semantic space (UMAP of the embeddings, HDBSCAN clusters).

The projection is cached in `landscape_cache`; this page only reads it. The Recompute button is the one
thing that computes (and writes the cache). No SQL here, all data comes from repka.dashboard_data.
"""

import html
import sqlite3
from contextlib import closing

import plotly.express as px
import streamlit as st

from repka import dashboard_data as data
from repka.config import ConfigError, db_path, load_config
from repka.domain.schema import load_domains
from repka.store.db import connect, migrate
from repka.textutil import printable, truncate

# dataviz palette (validated categorical slots 1-3, the most a scatter may colour), muted ink for the rest.
_SERIES = {"light": ["#2a78d6", "#eb6834", "#1baf7a"], "dark": ["#3987e5", "#d95926", "#199e70"]}
_SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}
_MUTED = "#898781"
_COLOURED = 3
_OTHER, _NOISE = "other clusters", "noise"

st.set_page_config(page_title="repka: landscape", layout="wide")
st.title("Landscape")

try:
    mode = "dark" if st.context.theme.type == "dark" else "light"
except Exception:  # theme info is not available in every runtime
    mode = "light"


def _group_names(frame):
    """Cluster name per repo with only the three biggest clusters kept; the rest fold into one group.

    Colour follows the cluster, not the filter: sizes come from the unfiltered frame.
    """
    sizes = frame[frame["cluster"] != -1].groupby("cluster_name").size().sort_values(ascending=False, kind="stable")
    keep = list(sizes.index[:_COLOURED])
    return keep, frame["cluster_name"].map(lambda n: n if n in keep else (_NOISE if n == "noise" else _OTHER))


def _figure(frame, keep, groups):
    frame = frame.assign(
        group=groups.loc[frame.index],
        origin=frame["is_fork"].map({True: "fork", False: "original"}),
        size=frame["stars"].fillna(0).clip(lower=0).pow(0.5).add(3),
        text=frame["description"].map(lambda d: html.escape(truncate(printable(d), 140))),
        label=frame["full_name"].map(lambda n: html.escape(printable(n))),
    )
    order = [*keep, *[g for g in (_OTHER, _NOISE) if g in set(frame["group"])]]
    colours = dict(zip(keep, _SERIES[mode], strict=False)) | {_OTHER: _MUTED, _NOISE: _MUTED}
    fig = px.scatter(
        frame, x="x", y="y", color="group", symbol="origin", size="size", size_max=22,
        color_discrete_map=colours, category_orders={"group": order, "origin": ["original", "fork"]},
        symbol_map={"original": "circle", "fork": "diamond"},
        custom_data=["html_url", "label", "text", "kind", "stars", "origin"],
    )
    fig.update_traces(
        marker={"line": {"width": 1.5, "color": _SURFACE[mode]}},
        hovertemplate="<b>%{customdata[1]}</b><br>%{customdata[2]}<br>kind: %{customdata[3]}"
        "<br>stars: %{customdata[4]} (%{customdata[5]})<extra></extra>",
    )
    for trace in fig.data:
        if trace.name.startswith(_NOISE):
            trace.marker.opacity = 0.4
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.update_layout(height=620, legend_title_text="cluster", margin={"l": 0, "r": 0, "t": 10, "b": 0})
    return fig


if not db_path().exists():
    st.info("No database yet. Run `repka db migrate`, then `repka run`.")
    st.stop()

try:
    domains = load_domains()
    model = load_config().embed.model
except ConfigError as exc:
    st.error(f"Configuration is invalid: {exc}")
    st.stop()
if not domains:
    st.info("No domains yet. Add one with `repka domain new`.")
    st.stop()

domain = st.selectbox("Domain", domains, format_func=lambda d: f"{printable(d.name)} ({d.id})")
forks_excluded = domain.search.fork_policy == "exclude"
if forks_excluded:
    st.info(
        "This domain's fork policy is `exclude`, so no forks were collected and none appear on the map. "
        "Change `fork_policy` to `include` on the [Domains page](/Domains) (domain editor), then run a scan, to see niche forks."
    )

col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
accepted_only = col1.checkbox("Accepted only", value=True)
include_forks = col2.checkbox("Include forks", value=not forks_excluded, disabled=forks_excluded)
min_stars = col3.number_input("Min stars", min_value=0, value=0, step=10)
recompute = col4.button("Recompute", help="Re-run the projection and clustering and store the result")

try:
    if recompute:
        with st.spinner("Projecting and clustering..."), closing(connect(readonly=True)) as ro:
            row = data.compute_landscape(ro, domain.id, model)
        if row is not None:
            with closing(connect()) as rw:
                migrate(rw)
                data.store_landscape(rw, row)
    with closing(connect(readonly=True)) as conn:
        frame, status = data.landscape_frame(conn, domain.id, model)
except sqlite3.Error as exc:
    st.error(f"Could not read the database (run `repka db migrate`?): {exc}")
    st.stop()

if status == "no_embeddings":
    st.info(
        f"No embeddings for model `{model}` in this domain yet. Set `embed.enabled: true` in `.config.yaml` "
        "and run `repka embed` (needs Ollama)."
    )
    st.stop()
if status == "not_computed":
    st.info("The map has not been computed yet. Press **Recompute**.")
    st.stop()
if status == "stale":
    st.warning("Embeddings changed since this map was computed. Press **Recompute** to refresh it.")

keep, groups = _group_names(frame)
view = frame
if accepted_only:
    view = view[view["accepted"] == True]  # noqa: E712  (NaN/None = undecided, not accepted)
if not include_forks:
    view = view[~view["is_fork"]]
view = view[view["stars"].fillna(0) >= min_stars]
if view.empty:
    st.info("No repos match these filters.")
    st.stop()

st.caption(f"{len(view)} of {len(frame)} repos, embedding model `{model}`. Size = stars, shape = fork or original.")
event = st.plotly_chart(_figure(view, keep, groups), on_select="rerun", selection_mode="points", key="landscape")
picked = [p["customdata"] for p in event.selection.points if p.get("customdata")] if event else []
for url, label, *_ in picked[:5]:
    if url.startswith("https://github.com/"):
        st.link_button(f"Open {html.unescape(label)} on GitHub", url)
if not picked:
    st.caption("Click a point to get a link to the repository.")

st.subheader("Clusters")
table = (
    view.groupby("cluster_name")
    .agg(repos=("repo_id", "size"), forks=("is_fork", "sum"), stars=("stars", "sum"))
    .sort_values("repos", ascending=False, kind="stable")
    .reset_index()
    .rename(columns={"cluster_name": "cluster"})
)
st.dataframe(table, hide_index=True)

st.subheader("Repositories")
st.dataframe(
    view[["full_name", "cluster_name", "kind", "stars", "is_fork", "html_url"]],
    hide_index=True,
    column_config={"html_url": st.column_config.LinkColumn("GitHub", display_text="open")},
)
