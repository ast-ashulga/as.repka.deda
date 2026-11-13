# Telegram delivery

[← Back to the README](../README.md)

## Telegram (digest delivery)

Digests are sent from your own Telegram account (default chat `"me"` = Saved Messages) through
`fast-mcp-telegram`. Nothing is sent unless `notify.enabled: true`.

```bash
# REPKA_TG_API_ID / REPKA_TG_API_HASH from https://my.telegram.org go into .env
uv run repka notify login                       # one time: phone, code, 2FA
uv run repka notify test "hello" --dry-run      # prints the message, sends nothing
uv run repka notify test "hello"                # real send (needs notify.enabled: true)
uv run repka digest                             # preview of the pending digest on stdout; sends and writes nothing
uv run repka digest --slot 19:30                # preview as that slot of today
uv run repka digest --send                      # deliver the due slot and record it (needs notify.enabled: true)
```

The session in `.sessions/` is full access to the account: never commit or share it. A slot is sent at most once;
missed slots are coalesced into the latest one, and a slot older than `digest.max_lateness` is skipped. An empty
digest is never sent. A failed send is retried on the next run.

### Insights digest and releases message (optional, off by default)

Two switches in `.config.yaml`, both `false` until you turn them on (they are meant to be used together; `repka doctor`
warns if only one is on, and neither is an error):

- `digest.insights: true`: the main digest shows only what is new or growing (🆕 🔎 📈, plus stack changes). Each
  item has a one-line description, `kind · lvl N` and up to three facet tags: the domain's extra decider answers
  with probability >= `digest.facet_min_p` (0.7). A legend line explains the icons. Items over
  `digest.max_items_per_domain` are counted in the footer (`…and 12 more: 5 🆕, 7 🔎`). New, found and release items
  stay pending until shown, or until `digest.pending_max_age` (7d) passes; stack changes and movers over the limit
  do not wait (stack changes are consumed unseen, movers are recomputed each time).
- `digest.releases_message: true`: releases (🚀) leave the main digest and go out once a day as a separate message at
  `digest.releases_slot` (09:00): per domain, newest release per repo with stars and tag, most stars first. An empty
  releases message is never sent, and a failed send keeps the releases for the next one.

```bash
uv run repka digest --more               # items waiting in the queue: new/found/release overflow, not stack/mover (read-only)
uv run repka releases                    # release events of accepted repos in the last 24h (read-only)
uv run repka releases --since 7d --domain example-mcp
```

The releases message is sent by `repka run` (not by `repka digest --send`, which delivers only the main digest). It
has its own row in the digest history, is sent at most once a day and never when empty, and the run summary has a
`releases` line. The
dashboard Home page lists both kinds of message, what is waiting and the recent releases (see
[Dashboard](dashboard.md)).

Without the two switches nothing changes: the digest keeps its current format and releases stay in it.

