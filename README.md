# claude-limit-primer

A tiny Telegram bot that keeps your **Claude Code subscription usage window
always ticking**. It runs on your server and, every 5 hours (anchored to your
reset time), sends a minimal `claude -p` request so the 5-hour limit clock
starts when *you* want it to — not only when you manually send the first message
of the day. It also pings you on Telegram every time it primes.

## Why

Claude Code's 5-hour limit window only starts counting from your **first**
request after a reset. If your limits reset overnight but you don't open Claude
until the morning, that fresh window hasn't started yet — so you lose part of it.
This bot starts the window on a schedule, so capacity lines up with your day.

The catch: you can only **restart** the clock with a request sent *after* the
current window has expired. Priming before expiry just spends a sliver of the
current window and resets nothing. So every prime is scheduled a few minutes
**after** the expected reset (`margin_minutes`), never before.

> Note: a true 5-hour cycle doesn't divide evenly into a 24-hour day
> (24 / 5 = 4.8), so fixed cron times would drift by an hour each day. The bot
> computes `next prime = previous prime + 5h` dynamically instead.

## How it works

One Python process (stdlib only, no dependencies) does both jobs:
- **Scheduler** — primes the limits when the window is due.
- **Telegram bot** — long-polls for commands so you can set/adjust everything
  from chat, and sends a notification on every prime.

## The schedule model

There is always **exactly one schedule**, and **your last command is the single
source of truth** — it fully replaces whatever was scheduled before. There are
no overlapping jobs, no cron entries, no leftover timers.

- `/prime` (or `/reset` with no time) means *"my limits just reset now"* — it
  primes immediately and chains every 5h from this moment.
- `/init HH:MM [Zone]` (or `/reset HH:MM`) schedules the **first** prime at a
  clock time; after it fires, the chain continues every 5h from each actual
  prime.

So pick whichever matches what just happened: hit `/prime` when you notice your
limits reset, or `/init` once if you know the clock time they reset at.

## Smart reset: `/plan`

`/plan` schedules a reset to land at a time **you** choose, so you get topped up
at the moment you want — instead of waiting for the chain's next boundary.

```
/plan 10:00                        # reset the window at 10:00
/plan 10:00 Europe/Moscow
```

**How it places the window.** The bot primes at `END − cycle` (e.g. `10:00 − 5h
= 05:00`), opening the window `[05:00, 10:00]`:

```
  05:00                               10:00
   │ prime opens                        │ window expires
   │ a fresh window                     │ → resets to full
   ▼                                    ▼
 ───●───────────────────────────────────●────►
   └──── window [05:00, 10:00] ─────────┘
```

- The window is **fresh** from 05:00 — only the primer's throwaway `pong` has
  touched it.
- At 10:00 it **expires → resets to full**, exactly the moment you asked for.

**The dormant gap is intentional.** A prime only *starts* a window; nothing
runs while idle. So between the moment the previous window expires and the
moment the plan's prime fires, **nothing primes** — the system is dormant: the
limits are full, but no window is open and the 5-hour countdown is **not**
running. `/plan` simply overwrites the single pending `next_prime` (see
[The schedule model](#the-schedule-model)); there is no second timer firing in
between.

### When `/plan` refuses (safety guard)

A request can start a fresh window **only after** the previous one expired. So
if your `END − 5h` falls *before* the current window expires, the plan's prime
would fire into a live window and just spend it — resetting nothing. The bot
detects this and refuses instead of silently setting a broken plan, telling you
the earliest time that works or to wait and retry:

> 🚫 Can't reset at 23:00: the current window is still live until Jul 02 19:27,
> but the plan needs to open a fresh window at 18:00 (= END − 5h) — earlier than
> that expiry. Plan no earlier than Jul 03 00:30, or wait until after 19:27 and
> set the plan again.

> ⚠️ The primer only controls **its own** primes. If you use Claude yourself
> during the dormant gap, your request starts a window and can throw the plan
> off. During the gap, just let it sleep.

**Typical use:** run `/plan 10:00` the evening before. The dormant gap is
simply overnight, and you wake up to a fresh window whose reset lands right
when you want it.

## Requirements

- [Claude Code CLI](https://claude.com/claude-code) installed and logged into
  your subscription (`claude -p "hi"` should work).
- Python 3.9+ (uses the stdlib `zoneinfo`).
- A Telegram bot token.

## Setup

### 1. Create a Telegram bot
1. Message **@BotFather** → `/newbot` → copy the **token**.
2. Put the token in `.env` (copy from `.env.example`):
   ```
   TELEGRAM_TOKEN=123456:ABC...
   ```
   You don't need the chat id — the bot captures it from your first message.

### 2. Run it (systemd --user, auto-restart)
```bash
cp .env.example .env        # then edit .env and paste your token
systemctl --user daemon-reload
systemctl --user enable --now claude-limit-primer
loginctl enable-linger "$USER"   # keep running after logout / reboot
```
Logs: `journalctl --user -u claude-limit-primer -f` (or see `primer.log`).

> Don't use systemd? Run `python3 primer.py bot` under tmux/nohup, or schedule
> `run-tick.sh` from cron (`* * * * *`) as a scheduler-only alternative.

### 3. Configure from chat
Send `/start` to your bot (it links your chat), then set your reset time:
```
/init 02:00 Europe/Moscow
```

## Bot commands

On startup the bot registers these with Telegram (`setMyCommands`), so they show
up in the `/` menu with descriptions.

| Command | What it does |
|---|---|
| `/prime` | limits reset now: prime & chain from now (single source of truth) |
| `/reset` | same as `/prime`; `/reset HH:MM` changes the clock time |
| `/init HH:MM [Zone]` | schedule the first prime at a clock time |
| `/plan END [Zone]` | reset the window at a chosen time (smart reset) |
| `/status` | current window and next prime |
| `/pause` / `/resume` | pause / resume auto-priming |
| `/cycle N` | window length in minutes (default 300) |
| `/margin N` | minutes after reset to prime (default 3) |
| `/tz Zone` | timezone (e.g. `Europe/Moscow`) |
| `/help` | help |

## CLI (same thing without chat)
```bash
python3 primer.py init --reset 02:00 --tz Europe/Moscow
python3 primer.py plan --end 10:00                # reset at 10:00
python3 primer.py status
python3 primer.py prime          # prime now
python3 primer.py test-telegram  # check notifications
```

## Configuration

`config.json` (created from `config.example.json`) holds non-secret settings:

| Key | Default | Meaning |
|---|---|---|
| `tz` | `UTC` | your timezone |
| `model` | `claude-haiku-4-5-20251001` | cheapest model for the ping |
| `cycle_minutes` | `300` | window length (5 h) |
| `margin_minutes` | `3` | how long after a reset to prime |
| `prompt` | `Reply with exactly one word: pong` | the throwaway prompt |
| `notify_on_prime` / `notify_on_failure` | `true` | Telegram notifications |

Secrets (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`) live only in `.env` and are never
written back into `config.json`.

## Security

The bot locks onto the first chat that messages it and ignores every other
chat. To re-link, clear `telegram_chat_id` in `config.json` (or set it in
`.env`) and restart.

## Cost

Each prime is one request to the cheapest model with a one-word reply — a few
thousand cached tokens, ~5 times a day. On a subscription that's negligible; it
just counts against your limits, which is the whole point.

## Files

| File | Purpose |
|---|---|
| `primer.py` | bot + scheduler (all logic) |
| `.env` | Telegram token (gitignored) |
| `config.json` | settings (gitignored; see `config.example.json`) |
| `state.json` | window state (auto-created) |
| `primer.log` | run log |
| `claude-limit-primer.service` | systemd --user unit |
| `run-tick.sh` | cron wrapper (scheduler-only alternative) |

## License

MIT — see [LICENSE](LICENSE).
