"""repka dashboard home: domains, last runs and last digests. Read-only.

Launch with `repka dashboard`. Pages in dashboard/pages/ use the same pattern: open a read-only
connection per rerun, call repka.dashboard_data, show the frame. No SQL lives here.
"""

import sqlite3
from contextlib import closing

import streamlit as st

from repka import dashboard_data as data
from repka.config import ConfigError, db_path, load_config, parse_duration
from repka.domain.schema import load_domains
from repka.store.db import connect


st.set_page_config(page_title="repka", layout="wide")
st.title("repka")
st.caption(f"Read-only view of {db_path()}. Data changes only when `repka run` (or collect) writes.")

if not db_path().exists():
    st.info("No database yet. Run `repka db migrate`, then `repka run`.")
    st.stop()

domains = []
try:
    domains = load_domains()
except ConfigError as exc:
    st.error(f"Domain files are invalid: {exc}")

try:
    with closing(connect(readonly=True)) as conn:
        st.subheader("Domains")
        if domains:
            st.dataframe(data.shown(data.domains_overview(conn, domains)), hide_index=True)
        else:
            st.info("No domains yet. Add one with `repka domain new`.")

        st.subheader("Last runs")
        st.dataframe(data.shown(data.recent_runs(conn, 10)), hide_index=True)

        st.subheader("Last digests")
        st.caption(
            "`main` is the digest; `releases` is the separate daily releases message "
            "(only when `digest.releases_message` is on)."
        )
        st.dataframe(data.shown(data.recent_digests(conn, 10)), hide_index=True)

        enabled = [d for d in domains if d.enabled]
        if enabled:
            try:
                digest_cfg = load_config().digest
            except ConfigError as exc:
                st.error(f"Settings are invalid: {exc}")
                digest_cfg = None
            if digest_cfg is not None:
                st.subheader("Waiting for the next digest")
                if digest_cfg.insights or digest_cfg.releases_message:
                    st.caption(
                        "Items kept until shown (as in `repka digest --more`); they expire after "
                        f"`digest.pending_max_age` ({digest_cfg.pending_max_age}). Releases are counted for the "
                        "releases message when `digest.releases_message` is on."
                    )
                    waiting = data.waiting_items(conn, enabled, digest_cfg)
                    if waiting.empty:
                        st.info("Nothing is waiting.")
                    else:
                        st.dataframe(waiting, hide_index=True)
                else:
                    st.info("Off: `digest.insights` and `digest.releases_message` are both false, so nothing is held back.")

            st.subheader("Recent releases")
            window = st.selectbox("Window", ["24h", "7d", "30d"], index=0)
            releases = data.recent_releases(conn, enabled, parse_duration(window))
            if releases.empty:
                st.info(f"No releases of accepted repos in the last {window}.")
            else:
                st.dataframe(data.shown(releases), hide_index=True)
except sqlite3.Error as exc:
    st.error(f"Could not read the database (run `repka db migrate`?): {exc}")
