# 🤖 AI agent setup guide

This file is a **runbook for an AI coding agent** (Claude Code, Cursor, etc.)
to install and run **claude-limit-primer** for a user, end-to-end. Follow it in
order, verify each step, and ask the user only for what they genuinely must
provide.

> Human? You probably want the regular **[Quick start](./README.md#quick-start)**
> in the README instead. This guide is written *for an agent*.

---

## What you're setting up

A small background service (one Python file, **zero `pip` dependencies**) that:

1. sends a throwaway `claude -p "pong"` request every ~5 hours so the user's
   Claude Code usage window opens on a schedule **they** control;
2. runs a Telegram bot so they can steer it (`/prime`, `/init`, `/plan`,
   `/status` …) and get pinged on every prime.

It runs as a `systemd --user` unit that auto-restarts and survives reboots.

---

## 0. Prerequisites — verify before touching anything

Run these checks. If any fail, fix or escalate to the user (see
[Troubleshooting](#troubleshooting)).

| Check | Command | Expected |
|---|---|---|
| Python ≥ 3.9 | `python3 --version` | `Python 3.9` or higher |
| Claude Code CLI present | `command -v claude` | a path, e.g. `/home/linuxbrew/.linuxbrew/bin/claude` |
| Claude CLI logged in | `claude -p "Reply with one word: ok"` | succeeds (subscription auth works) |
| systemd --user available | `systemctl --user status` | runs (Linux) |

> **No systemd (macOS, containers)?** Skip the unit and run
> `python3 primer.py bot` under tmux/nohup, or drive `run-tick.sh` from cron.
> See [Alternative: no systemd](#alternative-no-systemd).

---

## 1. Ask the user for these (and only these)

You need **three** pieces of input. Everything else has a sane default.

1. **Telegram bot token** — they get it from [@BotFather](https://t.me/BotFather)
   via `/newbot`. Looks like `123456:ABC-DEF...`.
   - **Do not** ask for a chat id — the bot captures it automatically from the
     user's first `/start`.
2. **Timezone** — e.g. `Europe/Moscow`, `Europe/Belgrade`, `America/New_York`.
   (Default `UTC`.)
3. **Anchor time** (optional) — a clock time their limits tend to reset at, e.g.
   `02:00`. If they don't know, they can just send `/prime` to the bot later
   the moment they notice a reset.

Never write the token into `config.json` — it lives **only in `.env`**.

---

## 2. Clone

```bash
git clone https://github.com/blackbalancef/claude-limit-primer.git ~/projects/claude-limit-primer
cd ~/projects/claude-limit-primer
```

> The bundled unit expects the repo exactly at `~/projects/claude-limit-primer`.
> If the user wants it elsewhere, clone there and **edit two paths** in
> `claude-limit-primer.service` (`WorkingDirectory=` and the `ExecStart=` path)
> to match — see step 4.

---

## 3. Configure `.env`

```bash
cp .env.example .env
```

Then write the user's token into `.env` (fill in the real value from step 1):

```env
TELEGRAM_TOKEN=PASTE_TOKEN_HERE
TELEGRAM_CHAT_ID=
```

Leave `TELEGRAM_CHAT_ID` empty — it auto-captures from the user's first message.

Optionally set the timezone now in `config.json` (`"tz": "Europe/Moscow"`), or
let the user send `/tz Europe/Moscow` to the bot later.

Sanity-check it parses:

```bash
python3 primer.py status      # should print status, not a traceback
```

---

## 4. Make sure `claude` is on the service's PATH

The unit hardcodes a PATH:

```
%h/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin
```

Check that `claude` (from step 0) is reachable under one of those. If it lives
elsewhere (nvm / asdf / a different global npm prefix), **add that directory** to
the `Environment=PATH=...` line in `claude-limit-primer.service`. Otherwise
priming will fail with *"claude CLI not found in PATH"*.

Verify from the service's perspective:

```bash
env PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin" command -v claude
```

---

## 5. Install + enable the systemd unit

```bash
mkdir -p ~/.config/systemd/user
cp claude-limit-primer.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-limit-primer
loginctl enable-linger "$USER"     # keep it running after logout / reboot
```

---

## 6. Verify it's alive and healthy

```bash
systemctl --user status claude-limit-primer        # Active: active (running)
journalctl --user -u claude-limit-primer -n 20 --no-pager   # "bot: started", no errors
python3 primer.py test-telegram                     # sends a test message IF chat is linked
```

The `test-telegram` will only deliver once the chat is linked (next step).

---

## 7. Link the chat + set the schedule (user action)

**You (the agent) cannot do this step** — it must come from the user's own
Telegram, because the bot binds to the first chat that messages it. Tell the
user to:

1. Open the bot in Telegram and send **`/start`** → the bot replies that the
   chat is linked.
2. Then send one of:
   - **`/prime`** — "my limits just reset now" (prime immediately + chain 5h);
     **or**
   - **`/init 02:00 Europe/Moscow`** — schedule the first prime at a clock time;
     **or**
   - **`/plan 10:00`** — land a reset at a chosen time (smart reset).
3. Send **`/status`** to confirm the window and next prime look right.

---

## 8. Confirm a real prime fires

Watch the log and wait for / trigger a prime:

```bash
journalctl --user -u claude-limit-primer -f
```

You should eventually see a line like:

```
PRIME OK (scheduled): reply='pong' (4.2s)
```

and the user gets a Telegram ping `✅ Limits primed`. You can also force one
for testing with `python3 primer.py prime`.

---

## ✅ Done criteria

All of the following are true:

- [ ] `systemctl --user is-active claude-limit-primer` → `active`
- [ ] `python3 primer.py status` shows a `Next prime` in the future
- [ ] user has sent `/start` and a schedule command; `/status` looks right to them
- [ ] at least one `PRIME OK` appears in the log (or a forced `prime` succeeds)
- [ ] the user received a Telegram notification

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `claude CLI not found in PATH` (in log) | The `claude` binary isn't on the unit's PATH. Add its dir to `Environment=PATH=` in the service, `daemon-reload`, `restart`. |
| `claude` not logged in (auth error) | Have the user run `claude` interactively once to authenticate their subscription. |
| `Telegram not configured` | `TELEGRAM_TOKEN` missing/empty in `.env`. Re-add it and `systemctl --user restart`. |
| `test-telegram` says FAILED | No chat linked yet — the user must `/start` the bot first. |
| Bot ignores the user's messages | A different chat is already linked. Clear `telegram_chat_id` in `config.json` (or set it in `.env`) and restart. |
| Unit won't start after moving the repo | The hardcoded path in the service no longer matches. Update `WorkingDirectory=` and `ExecStart=` paths, `daemon-reload`, `restart`. |
| Status shows "No anchor set" | No schedule yet — the user should send `/prime` or `/init HH:MM Zone`. |

---

## Alternative: no systemd

If `systemctl --user` isn't available, run the bot directly and keep it alive
manually:

```bash
nohup python3 primer.py bot >> primer.log 2>&1 &
```

…under `tmux`/`screen`, or write a macOS `launchd` plist. For a **scheduler-only**
setup (no bot), drive the tick from cron:

```cron
* * * * *  ~/projects/claude-limit-primer/run-tick.sh
```

Note: without the bot you lose Telegram control/notifications; `/init` via CLI
still works (`python3 primer.py init --reset 02:00 --tz Europe/Moscow`).
