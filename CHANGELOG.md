# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Domain-based monitoring of GitHub repositories (search slices plus GraphQL snapshots), with domains defined as YAML.
- Triage of new repositories by local decider models; relevance and maturity verdicts stored in SQLite.
- Local embeddings through Ollama, and a read-only Streamlit dashboard.
- Telegram digests at configured slots: new repos, star spikes, stack changes and releases.
- `repka doctor` preflight, `repka run` full pass, and a launchd schedule installed by an explicit command.
- JSON output and a non-interactive interface for use with AI agents.
- All feature flags are off by default.

### Changed

- Star spikes in the digest now also need a daily growth of at least `digest.star_spike_min_pct` percent (default 2.0, 0 = off), so large repos with ordinary daily growth no longer fill the "star spike" section.
- The digest footer shows the `repka digest --more` hint only when queued items (new, found, release) are among the hidden ones; stack and mover overflow is not kept.
