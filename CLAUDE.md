# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python service (`primer.py`, **stdlib only — no dependencies**)
that keeps a Claude Code *subscription* usage window alive. It sends a tiny
`claude -p` request on a 5-hour chain so the limit clock starts when the user
wants, and exposes a Telegram bot to control and observe it.

## Run / operate

```bash
# the service (long-running bot + scheduler)
systemctl --user restart claude-limit-primer
systemctl --user status claude-limit-primer
journalctl --user -u claude-limit-primer -f      # live logs (also primer.log)

# direct CLI (same logic, no chat) — useful for local testing
python3 primer.py status                 # inspect the single schedule
python3 primer.py prime                  # force a real prime now (hits claude)
python3 primer.py init --reset 02:00 --tz Europe/Moscow
python3 primer.py test-telegram          # verify notifications
python3 primer.py bot                    # run the bot in the foreground
```

There is no build/lint/test suite. After editing `primer.py`, syntax-check with
`python3 -c "import primer"` and restart the service to load changes.

## Architecture & invariants

One process does two jobs in `cmd_bot`'s loop: each iteration first runs
`tick_once` (prime if due) then long-polls Telegram `getUpdates` (25s) for
commands. So scheduling resolution is ~25s; that's fine.

- **Exactly one schedule, last command wins.** All scheduling state is the
  single `next_prime_epoch` in `state.json`. Both `set_anchor` (init/reset) and
  `do_prime` fully overwrite it — there is never more than one pending prime,
  and no cron/systemd-timer is involved. Preserve this invariant.
- **Prime strictly AFTER the reset.** You can only restart the window's clock
  with a request sent *after* the previous window expired. So
  `next_prime = next_reset + margin_minutes`, never before. Priming early just
  spends the live window and resets nothing.
- **5h doesn't divide 24h** (24/5 = 4.8), so fixed wall-clock times drift. The
  chain is computed dynamically (`do_prime`: `next = now + cycle + margin`).
  `set_anchor` only sets the *first* prime at a clock time; the chain takes over
  after.
- **Secrets live only in `.env`.** `load_config` overlays `TELEGRAM_TOKEN` /
  `TELEGRAM_CHAT_ID` from `.env`; `save_config` strips them back out so they are
  never written into `config.json`. Keep this when adding settings.
- **Single-chat lock.** The bot binds to the first chat that messages it
  (auto-captured into `config.json`) and ignores all others.

## Files

`config.json`, `state.json`, `.env`, and `*.log` are gitignored (local runtime
state / secrets). Committed templates are `config.example.json` / `.env.example`.
The systemd unit (`claude-limit-primer.service`) uses `%h` and installs to
`~/.config/systemd/user/`.
