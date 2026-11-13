# Research: Telegram delivery and scheduling on macOS

[← Wiki home](../README.md) · Related: [ADR-005/006](../spec/decisions.md)

Status: researched 2026-11-07 (mostly from the reference project and prior knowledge; **(verify)** marks API details to re-check).

## 1. Two ways to send to Telegram

| | Bot API (later option) | User account via `fast-mcp-telegram` (**chosen**, as in the reference) |
|---|---|---|
| Credentials | one bot token from @BotFather (`REPKA_TG_BOT_TOKEN`) | `api_id`/`api_hash` + interactive login, session file = **full account access** |
| Transport | `POST https://api.telegram.org/bot<token>/sendMessage` via httpx | MCP stdio subprocess, guarded tool allowlist |
| Fits a one-way digest? | yes: a bot posting into a chat/channel it was added to | yes, but heavier and broader access than needed |
| Reading chats | not needed | needed in the reference (it reads chats) |

Decision ([ADR-005](../spec/decisions.md#adr-005-telegram-via-the-users-own-account-fast-mcp-telegram-ported-from-the-reference), revised 2026-11-07 by the user): **user account** through `fast-mcp-telegram`, default chat `"me"` (Saved Messages), configurable `chat_id` / `topic_id`. Port of the reference's guarded MCP sender, allowlist `send_message` only. The Bot API details below are kept for a possible later backend.

### Bot API details (not used in Phase 1; verify if a bot backend is added)

- `sendMessage` params: `chat_id` (numeric id or `@channelusername`), `text` (≤ **4096** chars after entity parsing), `parse_mode: "HTML"`, `link_preview_options: {"is_disabled": true}` (older: `disable_web_page_preview`), optional `message_thread_id` for forum topics.
- HTML mode supports `<b> <i> <a href> <code> <pre>`; everything else must be escaped (`&lt; &gt; &amp;`). All repo-provided text is HTML-escaped (untrusted).
- 429 responses include `parameters.retry_after` seconds → sleep and retry once; then fail the send.
- Find the chat id: send a message to the bot, then `getUpdates`; the CLI offers `repka notify --whoami` style helper (`getMe` + `getUpdates` printing chat ids).
- Splitting: split on line boundaries so each part ≤ 4096; never split inside an HTML tag (build parts from whole digest lines).

## 2. Reused safety rules from the reference

- Sending is **off by default**: `notify.enabled: false` (must be a real YAML boolean) **and** `run.send: false`. `--dry-run` prints the exact message.
- Text from external sources: strip control characters before terminal output, HTML-escape before Telegram.
- Stop a delivery after 2 consecutive send failures.

## 3. Scheduling ("morning/evening", no real-time alerts)

- **launchd** user agent (`~/Library/LaunchAgents/com.repka.run.plist`), not cron (cron misses runs while asleep; launchd coalesces missed `StartCalendarInterval` events into one run on wake).
- One agent runs `repka run` **hourly** (`StartInterval 3600`). `repka run` decides itself:
  1. which domains are *due* for a scan (`now − last_scan_at >= domain.scan_every`);
  2. whether a digest slot is due.
- Digest slots: `digest.slots: ["08:30", "19:30"]` (local time). A slot is due when `now >= slot_time_today` and no row in `digests` for `(slot, local_date)` exists. **Coalescing:** if several slots are due (Mac was asleep), send only the most recent one and mark the earlier ones `skipped`. Slots older than `digest.max_lateness` (default 6h) are marked `skipped` too, so a Mac waking at 23:00 doesn't send the morning digest.
- **No-spam rule:** a digest with zero items is not sent; the slot is still recorded (`item_count = 0`, `sent_at = null`, status `empty`).
- `repka schedule install|uninstall|status` writes/removes the plist and calls `launchctl bootstrap/bootout gui/$UID` **(verify flags)**. Installing is an explicit command (never automatic).
- Overlap: `repka run` holds an flock on `.state/run.lock`; a second instance exits immediately with "another run is in progress" (exit 0, logged).

## Sources

- The earlier private prototype's README (launchd section, notifier rules) and notifier module (not published).
- Telegram Bot API (`sendMessage`, formatting, 4096 limit, `retry_after`): prior knowledge **(verify against core.telegram.org/bots/api)**.
- launchd semantics (`StartCalendarInterval` coalescing): prior knowledge **(verify against `man launchd.plist`)**.
