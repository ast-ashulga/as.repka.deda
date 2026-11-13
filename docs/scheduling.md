# Scheduling (launchd)

[← Back to the README](../README.md)

## Scheduling (launchd)

Nothing is scheduled until you ask for it. `repka schedule install` writes a launchd user agent that runs
`repka run` every hour (or, with `--at`, at fixed local times every day; a run missed while the Mac slept starts on
wake). A run only does work for domains that are due (`scan_every`), so an hourly agent is cheap; it sends digests
only when `notify.enabled` and `run.send` are both true. A run takes the run lock, so it never overlaps a manual one.

```bash
uv run repka schedule install --dry-run   # print the plist, change nothing
uv run repka schedule install             # every hour (--interval 30m to change it); replaces an old agent
uv run repka schedule install --at 22:00,09:00,12:00   # daily at these local times instead (exclusive with --interval)
uv run repka schedule status
uv run repka schedule uninstall
```

The plist holds no secrets: only `PATH` (so launchd can find `gh`; launchd's default PATH lacks `/opt/homebrew/bin`) and, when set at install time, `REPKA_HOME`. Re-run `schedule install` after moving `gh`/`uv` or changing `REPKA_HOME`. Logs go to `logs/launchd.log`.

