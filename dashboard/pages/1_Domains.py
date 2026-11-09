"""Domains page: list the domain files, create or edit one, enable or disable it, change its scan
interval, and delete it.

Every write goes through repka.domain.schema (save_domain, delete_domain) after the same checks the
`repka domain new` wizard makes. A form that fails a check writes nothing. Saving does not touch launchd:
the next `repka run` reads the domain files again. The database is only read (read-only connection).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import replace

import streamlit as st

from repka import dashboard_data as data
from repka.config import ConfigError, db_path, domains_dir
from repka.domain.matrix import build_queries
from repka.domain.schema import (
    FORK_POLICIES,
    Domain,
    SearchSpec,
    delete_domain,
    format_duration,
    load_domains,
    save_domain,
)
from repka.domain.suggest import suggest_keywords
from repka.domain.wizard import DEFAULT_MIN_STARS, _parse_id, _parse_name, _parse_scan_every, _terms
from repka.store.db import connect
from repka.textutil import printable

NEW = "(new domain)"
DEFAULT_SCAN_EVERY = "12h"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")


def _groups_text(domain: Domain) -> str:
    return "\n".join(", ".join(group) for group in domain.search.keywords)


def _domain_from_form(
    existing: Domain | None,
    *,
    name: str,
    domain_id: str,
    profile: str,
    groups: str,
    topics: str,
    languages: str,
    excludes: str,
    min_stars: int,
    fork_policy: str,
    scan_every: str,
    enabled: bool,
) -> Domain:
    """The form's values as a Domain. Raises ConfigError naming the field that is wrong."""
    name = _parse_name(name)
    if existing is None:
        domain_id = _parse_id(domain_id.strip() or _slug(name))
    profile = profile.strip()
    if not profile:
        raise ConfigError("description must not be empty")
    keyword_groups = tuple(group for group in (tuple(_terms(line)) for line in groups.splitlines()) if group)
    topic_terms = tuple(_terms(topics))
    if not keyword_groups and not topic_terms:
        hint = "; ".join(group[0] for group in suggest_keywords(profile)[:3])
        suffix = f" (suggested from the description: {hint})" if hint else ""
        raise ConfigError(f"keyword groups: add at least one group or topic{suffix}")
    search = {
        "keywords": keyword_groups,
        "topics": topic_terms,
        "languages": tuple(_terms(languages)),
        "exclude": tuple(_terms(excludes)),
        "min_stars": min_stars,
        "fork_policy": fork_policy,
    }
    scan = _parse_scan_every(scan_every.strip())
    if existing is None:
        return Domain(
            id=domain_id, name=name, enabled=enabled, scan_every=scan, profile=profile, search=SearchSpec(**search)
        )
    # Editing keeps what the form does not show (in-fields, key files, triage questions) and the id.
    return replace(
        existing,
        name=name,
        enabled=enabled,
        scan_every=scan,
        profile=profile,
        search=replace(existing.search, **search),
    )


def _delete(domain_id: str, target: str) -> None:
    """Button callback: deletes only when the confirmation box is ticked."""
    if not st.session_state.get(f"{target}:confirm_delete"):
        st.session_state["flash"] = ("error", "Tick the confirmation box first, then delete again.")
        return
    try:
        delete_domain(domain_id)
    except (ConfigError, OSError) as exc:
        st.session_state["flash"] = ("error", printable(str(exc)))
        return
    st.session_state["target"] = NEW
    st.session_state["flash"] = (
        "success",
        f"Deleted domains/{domain_id}.yaml. Its history stays in the database.",
    )


st.set_page_config(page_title="repka: domains", layout="wide")
st.title("Domains")
st.caption("Domain files live in domains/. The next `repka run` reads them; saving schedules nothing.")

flash = st.session_state.pop("flash", None)
if flash:
    getattr(st, flash[0])(flash[1])

try:
    domains = load_domains()
except ConfigError as exc:
    st.error(f"A domain file is invalid: {printable(str(exc))}. Fix or remove that file to see the list.")
    domains = []

st.subheader("Domains")
if not domains:
    st.info("No domains yet. Create one below.")
elif db_path().exists():
    try:
        with closing(connect(readonly=True)) as conn:
            st.dataframe(data.domains_overview(conn, domains), hide_index=True)
    except sqlite3.Error as exc:
        st.error(f"Could not read the database (run `repka db migrate`?): {printable(str(exc))}")
else:
    st.dataframe(
        [
            {
                "id": d.id,
                "name": printable(d.name),
                "enabled": d.enabled,
                "scan_every": format_duration(d.scan_every),
            }
            for d in domains
        ],
        hide_index=True,
    )
    st.caption("No database yet, so there is no scan status. `repka run` creates it.")

st.subheader("Edit or create")
by_id = {d.id: d for d in domains}
target = st.selectbox(
    "Domain", [NEW, *by_id], key="target", help="Pick a domain to edit, or create a new one."
)
existing = by_id.get(target)
key = f"{target}:"
fork_options = list(FORK_POLICIES)

with st.form(key=f"form:{target}"):
    name = st.text_input(
        "Name",
        value=existing.name if existing else "",
        key=f"{key}name",
        help="Shown in the dashboard and digests, e.g. AI agent harnesses.",
    )
    domain_id = st.text_input(
        "Id",
        value=existing.id if existing else "",
        key=f"{key}id",
        disabled=existing is not None,
        help="File name and key: 2-40 characters of a-z, 0-9 and '-'. Fixed once created; empty = from the name.",
    )
    profile = st.text_area(
        "Description",
        value=existing.profile if existing else "",
        key=f"{key}profile",
        help="What is in and out of the domain. Used by the triage decider.",
    )
    groups = st.text_area(
        "Keyword groups",
        value=_groups_text(existing) if existing else "",
        key=f"{key}groups",
        help="One query per line. Terms on one line are ORed, e.g. `mcp, model context protocol`.",
    )
    topics = st.text_input(
        "GitHub topics",
        value=", ".join(existing.search.topics) if existing else "",
        key=f"{key}topics",
        help="Comma-separated. Optional when there are keyword groups.",
    )
    languages = st.text_input(
        "Languages",
        value=", ".join(existing.search.languages) if existing else "",
        key=f"{key}languages",
        help="Comma-separated, e.g. Python, Go. Empty = any language.",
    )
    excludes = st.text_input(
        "Exclude words",
        value=", ".join(existing.search.exclude) if existing else "",
        key=f"{key}excludes",
        help="Comma-separated words to leave out of results (at most 5).",
    )
    min_stars = st.number_input(
        "Minimum stars",
        value=existing.search.min_stars if existing else DEFAULT_MIN_STARS,
        min_value=0,
        step=1,
        key=f"{key}min_stars",
        help="Repos with fewer stars are not searched.",
    )
    fork_policy = st.selectbox(
        "Forks",
        fork_options,
        index=fork_options.index(existing.search.fork_policy) if existing else 0,
        key=f"{key}fork_policy",
        help="exclude: no forks; include: forks too; only: forks only.",
    )
    scan_every = st.text_input(
        "Scan every",
        value=format_duration(existing.scan_every) if existing else DEFAULT_SCAN_EVERY,
        key=f"{key}scan_every",
        help="E.g. 12h or 7d; at least 1h. Applies from the next repka run.",
    )
    enabled = st.checkbox(
        "Enabled",
        value=existing.enabled if existing else False,
        key=f"{key}enabled",
        help="Off: the domain is saved but not scanned. New domains start off.",
    )
    preview_clicked = st.form_submit_button("Check and preview queries")
    save_clicked = st.form_submit_button("Save domain", type="primary")

    if preview_clicked or save_clicked:
        try:
            domain = _domain_from_form(
                existing,
                name=name,
                domain_id=domain_id,
                profile=profile,
                groups=groups,
                topics=topics,
                languages=languages,
                excludes=excludes,
                min_stars=int(min_stars),
                fork_policy=fork_policy,
                scan_every=scan_every,
                enabled=enabled,
            )
            queries = build_queries(domain)  # ConfigError names the term that does not fit GitHub's limits
            if save_clicked and existing is None and (domains_dir() / f"{domain.id}.yaml").exists():
                raise ConfigError(f"id: domain {domain.id!r} already exists; pick another id")
        except ConfigError as exc:
            st.error(printable(str(exc)))
        else:
            st.caption(f"{len(queries)} GitHub queries:")
            st.dataframe(
                [
                    {"#": n, "key": q.key, "language": q.language or "any", "query": printable(q.q)}
                    for n, q in enumerate(queries, 1)
                ],
                hide_index=True,
            )
            if save_clicked:
                try:
                    path = save_domain(domain)
                except (ConfigError, OSError) as exc:
                    st.error(printable(str(exc)))
                else:
                    state = "enabled" if domain.enabled else "saved disabled (not scanned until enabled)"
                    st.success(f"Wrote {path.name}: {state}. The next repka run picks it up.")

if existing is not None:
    st.divider()
    st.subheader(f"Delete {existing.id}")
    st.caption("Removes domains/<id>.yaml. Collected repos, verdicts and runs stay in the database.")
    st.checkbox("I want to delete this domain file", key=f"{key}confirm_delete")
    st.button(
        "Delete domain",
        key=f"{key}delete",
        on_click=_delete,
        args=(existing.id, target),
    )
