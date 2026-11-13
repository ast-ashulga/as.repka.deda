# Using repka with AI agents

[← Back to the README](../README.md)

## Quick start (AI agents)

Everything is non-interactive except `domain new` and `notify login`. stdout carries results, stderr carries
progress and `error: <message>`; exit 0 ok, 1 runtime failure, 2 fixable usage or configuration error.

```bash
uv run repka doctor --json           # {ok, errors, warnings, checks[{id,status,title,fix}]}: run the `fix` of each error
uv run repka domain new --from-file brief.txt < answers.txt   # or use the Claude skill below
uv run repka domain list; uv run repka domain show <id>; uv run repka domain plan <id> --json
uv run repka run --json              # full report (stages, errors, hints, digest text) on stdout
uv run repka digest --more           # items waiting for a later digest (read-only); repka releases lists release events
uv run repka db stats                # row counts and the last runs
uv run repka db prune [--apply]      # thin old snapshots, drop repos below a domain's min_stars, shrink cached READMEs, VACUUM (dry run by default)
```

- The dashboard is for humans. Do not launch it unless asked: it is a long-running server (`uv run repka dashboard`).
  If it must run, start it in the background with a scratch `REPKA_HOME`. `doctor` and `--json` output are the agent interfaces.

- The `domain-profile` skill (`.claude/skills/domain-profile/SKILL.md`) interviews the user, researches GitHub
  read-only and writes a validated `domains/<id>.yaml`. It may write only that file.
- Run any `repka` command in a scratch home with `REPKA_HOME=/some/dir` (config, `.env`, `domains/`, `.state/`,
  `logs/` and `deciders/` are all looked up there), which is how to rehearse without touching the real setup.
- Never enable `notify.enabled`, `run.send` or `repka schedule install` on the user's behalf, and never read
  `.env` or `.sessions/`: they hold secrets. A human decides those switches.
- Hints at the end of `repka run` (text and `hints` in `--json`) explain why a pass did less than expected.

