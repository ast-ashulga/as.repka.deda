# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
[Report a vulnerability](https://github.com/ast-ashulga/as.repka.deda/security/advisories/new)
(repository **Security** tab, then **Report a vulnerability**). Do not open a public issue for a security problem.

Include what you found, how to reproduce it and the impact. Expect an acknowledgement within a few days; this is a
small project maintained on a best-effort basis.

## Scope

repka is a local tool that runs on your own Mac. The areas that matter most:

- **Secrets**: the GitHub token, Telegram credentials (`.env`) and the Telegram session (`.sessions/`). They must
  not leak into logs, the SQLite database, digests or worker subprocesses.
- **Untrusted text**: repository names, descriptions, topics, READMEs and release names come from the internet and
  reach the terminal, Telegram messages and local model prompts. Escaping bypasses (terminal control sequences,
  HTML injection in digests) and prompt-injection paths that change what is sent or executed are in scope.
- **Local execution**: decider workers, the launchd agent and file paths derived from external input.

Out of scope: issues that need an attacker who already controls your machine or your `.env`, and vulnerabilities
in third-party dependencies that are already fixed upstream (please report those upstream).

## Supported versions

repka is pre-1.0. Only the latest commit on `main` is supported.
