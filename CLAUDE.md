# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small, strictly-typed Python package (`primer/`) that keeps a Claude Code
*subscription* usage window alive. It sends a tiny `claude -p` request on a
5-hour chain so the limit clock starts when the user wants, and exposes a
Telegram bot to control and observe it.

Stack: **aiogram 3** (Telegram bot), **pydantic v2** (`Config`/`State` models)
+ **pydantic-settings** (`.env` secrets), **loguru** (logging with rotation),
**tenacity** (transient retries). Managed with **uv** (Python ≥ 3.12).

## Run / operate

```bash
uv sync                                  # install deps + dev tools into .venv

# the service (long-running bot + scheduler)
systemctl --user restart claude-limit-primer
systemctl --user status claude-limit-primer
journalctl --user -u claude-limit-primer -f      # live logs (also primer.log)

# direct CLI (same logic, no chat) — useful for local testing
uv run primer status                 # inspect the single schedule
uv run primer prime                  # force a real prime now (hits claude)
uv run primer init --reset 02:00 --tz Europe/Moscow
uv run primer test-telegram          # verify notifications
uv run primer bot                    # run the bot in the foreground
```

## Check suite

There is no test suite; these must all pass after any change:

```bash
uv run ruff check . && uv run ruff format --check . \
  && uv run mypy primer && uv run ty check primer
```

ruff runs with `select = ["ALL"]` (short, commented ignore list in
`pyproject.toml`), mypy in `strict` mode. Then restart the service to load
changes.

## Package layout

- `primer/cli.py` — argparse CLI (`primer` entry point; subcommands `bot`,
  `init`, `plan`, `tick`, `prime`, `status`, `test-telegram`)
- `primer/bot.py` — aiogram bot: all `/commands`, single-chat lock, chat
  auto-capture, plus the background scheduler task (tick every ~25 s)
- `primer/schedule.py` — `set_anchor`, `set_plan` (+ its safety guard),
  `status_text`, `fmt_dur`
- `primer/prime.py` — `run_prime` (the `claude -p` subprocess,
  tenacity-retried), `do_prime`, `tick_once`
- `primer/settings.py` — `Config` (config.json) + `Secrets` (.env),
  `load_config` / `save_config`
- `primer/state.py` — `State` (state.json) + atomic JSON writes
- `primer/notify.py` — plain Telegram notifications (never raise into priming)
- `primer/paths.py` — data files live in the repo root (override:
  `PRIMER_DATA_DIR`)
- `primer/logging_setup.py` — loguru sinks: stdout + rotating `primer.log`

## Architecture & invariants

One process does two jobs in `primer bot`: a background asyncio task runs
`tick_once` (prime if due) every ~25 s while aiogram long-polls Telegram for
commands. So scheduling resolution is ~25s; that's fine.

- **Exactly one schedule, last command wins.** All scheduling state is the
  single `next_prime_epoch` in `state.json`. `set_anchor` (init/reset),
  `set_plan` and `do_prime` fully overwrite it — there is never more than one
  pending prime, and no cron/systemd-timer is involved. Preserve this invariant.
- **Prime strictly AFTER the reset.** You can only restart the window's clock
  with a request sent *after* the previous window expired. So
  `next_prime = next_reset + margin_minutes`, never before. Priming early just
  spends the live window and resets nothing.
- **5h doesn't divide 24h** (24/5 = 4.8), so fixed wall-clock times drift. The
  chain is computed dynamically (`do_prime`: `next = now + cycle + margin`).
  `set_anchor` only sets the *first* prime at a clock time; the chain takes over
  after.
- **Two retry layers, keep both.** tenacity handles transient blips (2–3 quick
  attempts inside `run_prime` / Telegram calls); the scheduler handles real
  outages (`failure_retry_minutes` re-prime + `retry_after_failure` state).
- **Secrets live only in `.env`.** `load_config` overlays `TELEGRAM_TOKEN` /
  `TELEGRAM_CHAT_ID` from `.env` (via pydantic-settings); `save_config` strips
  them back out so they are never written into `config.json`. Keep this when
  adding settings.
- **Single-chat lock.** The bot binds to the first chat that messages it
  (auto-captured into `config.json`) and ignores all others.
- **On-disk formats are stable.** `config.json` / `state.json` written by the
  old single-file version must keep loading without migration.

## Files

`config.json`, `state.json`, `.env`, `*.log` and `.venv/` are gitignored (local
runtime state / secrets). Committed templates are `config.example.json` /
`.env.example`. The systemd unit (`claude-limit-primer.service`) uses `%h`,
runs `.venv/bin/primer bot`, and installs to `~/.config/systemd/user/`.
