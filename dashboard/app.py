"""repka dashboard home: domains, last runs and last digests. Read-only.

Launch with `repka dashboard`. Pages in dashboard/pages/ use the same pattern: open a read-only
connection per rerun, call repka.dashboard_data, show the frame. No SQL lives here.
"""

import sqlite3
from contextlib import closing

import streamlit as st

from repka import dashboard_data as data
from repka.config import ConfigError, db_path
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
        st.dataframe(data.shown(data.recent_digests(conn, 10)), hide_index=True)
except sqlite3.Error as exc:
    st.error(f"Could not read the database (run `repka db migrate`?): {exc}")
