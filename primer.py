#!/usr/bin/env python3
"""
claude-limit-primer
===================
Keeps your Claude Code subscription usage window *always ticking* by sending a
tiny request on a schedule, so the 5-hour limit clock starts at a time YOU
control instead of only when you manually send the first message of the day.
Everything is controllable from a Telegram bot - including re-adjusting the
anchor time on the fly if the schedule ever drifts.

Why this is needed
------------------
Claude Code's 5-hour limit window only begins counting from your FIRST request
after a reset. If your limits reset at night but you don't touch Claude until
morning, the fresh window hasn't started yet. Priming it on a schedule chains
the window every 5 hours from an anchor time you provide.

Key timing rule
---------------
You can only RESTART the clock by priming AFTER the current window has expired.
So every prime is scheduled a few minutes *after* the expected reset
(margin_minutes), never before.

Run modes
---------
  primer.py bot                       run the Telegram bot + scheduler (main)
  primer.py init --reset HH:MM --tz Z set anchor from the CLI
  primer.py plan --start HH:MM --end HH:MM  reset at end of a work session
  primer.py tick                      prime once if due (cron alternative)
  primer.py prime                     force a prime now
  primer.py status                    print window state
  primer.py test-telegram            send a test notification

No third-party dependencies (urllib + stdlib only).
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DIR = Path(__file__).resolve().parent
CONFIG_PATH = DIR / "config.json"
STATE_PATH = DIR / "state.json"
LOG_PATH = DIR / "primer.log"
ENV_PATH = DIR / ".env"

DEFAULT_CONFIG = {
    "telegram_token": "",          # from @BotFather (prefer .env)
    "telegram_chat_id": "",        # auto-captured on first message if empty
    "tz": "UTC",                   # your timezone, e.g. Europe/Moscow
    "model": "claude-haiku-4-5-20251001",
    "cycle_minutes": 300,          # 5-hour window
    "margin_minutes": 3,           # prime this many minutes AFTER the reset
    "prompt": "Reply with exactly one word: pong",
    "claude_timeout_secs": 120,
    "notify_on_prime": True,
    "notify_on_failure": True,
}


# --------------------------------------------------------------------------- #
# config / state helpers
# --------------------------------------------------------------------------- #
def load_env() -> dict:
    """Tiny KEY=VALUE .env parser (no dependency)."""
    env = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    # .env overrides config (secrets live here, never in config.json)
    env = load_env()
    if env.get("TELEGRAM_TOKEN"):
        cfg["telegram_token"] = env["TELEGRAM_TOKEN"]
    if env.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = env["TELEGRAM_CHAT_ID"]
    return cfg


def save_config(cfg: dict) -> None:
    # Never persist secrets that are sourced from .env back into config.json.
    env = load_env()
    out = dict(cfg)
    if env.get("TELEGRAM_TOKEN"):
        out["telegram_token"] = ""
    if env.get("TELEGRAM_CHAT_ID"):
        out["telegram_chat_id"] = ""
    CONFIG_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def tzinfo(cfg: dict) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.get("tz", "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def fmt(epoch, cfg: dict) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, tzinfo(cfg)).strftime("%Y-%m-%d %H:%M %Z")


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}] {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
        lines = LOG_PATH.read_text().splitlines()
        if len(lines) > 500:
            LOG_PATH.write_text("\n".join(lines[-500:]) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------------- #
def tg_api(token: str, method: str, params: dict, timeout: int = 30):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def tg_send(cfg: dict, text: str, chat_id=None) -> bool:
    token = cfg.get("telegram_token", "")
    chat_id = chat_id or cfg.get("telegram_chat_id", "")
    if not token or not chat_id:
        log("Telegram not configured (no token/chat_id) - skipping notification")
        return False
    try:
        res = tg_api(token, "sendMessage", {
            "chat_id": chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }, timeout=20)
        if not res.get("ok"):
            log(f"Telegram sendMessage ok=false: {res}")
        return res.get("ok", False)
    except Exception as e:  # noqa: BLE001 - notifications must never crash priming
        log(f"Telegram send failed: {e}")
        return False


# --------------------------------------------------------------------------- #
# scheduling core
# --------------------------------------------------------------------------- #
def set_anchor(cfg: dict, reset_str: str, tz: str | None = None) -> dict:
    """Set the anchor reset time-of-day and compute the next prime."""
    if tz:
        cfg["tz"] = tz
        save_config(cfg)
    tz_i = tzinfo(cfg)
    now = datetime.now(tz_i)
    hh, mm = map(int, reset_str.strip().split(":"))
    reset_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    margin = timedelta(minutes=cfg["margin_minutes"])
    # First prime lands on the next occurrence of the given clock time.
    # After that, do_prime() chains every cycle from the actual prime moment.
    first_prime = reset_today + margin
    while first_prime <= now:
        first_prime += timedelta(days=1)

    state = load_state()
    state.update({
        "anchor_reset": reset_today.isoformat(),
        "next_reset_epoch": (first_prime - margin).timestamp(),
        "next_prime_epoch": first_prime.timestamp(),
        "paused": False,
    })
    state.setdefault("last_prime_epoch", None)
    save_state(state)
    return state


class PlanError(ValueError):
    """Raised when a /plan target cannot be reached safely - the plan's prime
    would fire into a still-live window and fail to reset it."""


def set_plan(cfg: dict, start_str: str, end_str: str, tz: str | None = None) -> dict:
    """Schedule a prime so the limit window RESETS exactly at the session END.

    The window covering the session becomes [END - cycle, END]: it is fresh at
    START (only the primer touched it since END - cycle) and expires - i.e.
    resets to full - right at END. Heavy usage during the session is therefore
    topped up the moment you finish, instead of leaving you locked out for
    hours waiting for the next boundary.

    This is the "smart reset": align the window boundary with the end of your
    planned work, avoiding the worst case where a reset fires right after you
    start (which hands you a full 5h window you then have to stretch out).
    """
    if tz:
        cfg["tz"] = tz
        save_config(cfg)
    tz_i = tzinfo(cfg)
    now = datetime.now(tz_i)
    sh, sm = map(int, start_str.strip().split(":"))
    eh, em = map(int, end_str.strip().split(":"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        raise ValueError("end must be after start")

    cycle = timedelta(minutes=cfg["cycle_minutes"])
    margin = timedelta(minutes=cfg["margin_minutes"])

    # Open the session window with a prime at END - cycle so it expires exactly
    # at END. Roll forward a day at a time until that prime is in the future
    # (a prime can only be scheduled, never back-dated).
    target_prime = end - cycle
    while target_prime <= now:
        start += timedelta(days=1)
        end += timedelta(days=1)
        target_prime += timedelta(days=1)

    # GUARD: a request can only START (or reset) a window if the previous one
    # has already expired. The plan's prime lands at target_prime (= END -
    # cycle); if the currently-live window is still alive then, the prime would
    # merely spend it (a tiny "pong") and reset nothing - so the boundary would
    # miss END entirely. Refuse rather than silently set a broken plan.
    state = load_state()
    cur_reset_epoch = state.get("next_reset_epoch")
    if cur_reset_epoch:
        cur_reset = datetime.fromtimestamp(cur_reset_epoch, tz_i)
        if cur_reset > now and cur_reset + margin > target_prime:
            earliest = cur_reset + cycle + margin
            raise PlanError(
                f"Can't reset at {end:%H:%M}: the current window is still live "
                f"until {cur_reset:%b %d %H:%M}, but the plan needs to open a "
                f"fresh window at {target_prime:%H:%M} (= END - "
                f"{cfg['cycle_minutes'] // 60}h) - earlier than that expiry. "
                f"Plan a session ending no earlier than {earliest:%b %d %H:%M}, "
                f"or wait until after {cur_reset:%H:%M} and set the plan again."
            )

    state.update({
        "plan_start": start.isoformat(),
        "plan_end": end.isoformat(),
        "next_reset_epoch": (target_prime - margin).timestamp(),
        "next_prime_epoch": target_prime.timestamp(),
        "paused": False,
    })
    state.setdefault("last_prime_epoch", None)
    save_state(state)
    return state


def status_text(cfg: dict, state: dict) -> str:
    if not state.get("next_prime_epoch"):
        return ("No anchor set. Use <code>/init HH:MM [Zone]</code>\n"
                "e.g. <code>/init 02:00 Europe/Moscow</code>")
    now = time.time()
    paused = state.get("paused")
    lines = [
        "📊 <b>Primer status</b>",
        f"🌍 Timezone: {cfg['tz']}",
        f"🕐 Now: {fmt(now, cfg)}",
        f"♻️ Cycle: every {cfg['cycle_minutes']/60:.1f} h "
        f"(prime {cfg['margin_minutes']} min after reset)",
    ]
    if state.get("last_prime_epoch"):
        ok = "OK" if state.get("last_prime_ok") else "FAIL"
        lines.append(f"✔️ Last prime: {fmt(state['last_prime_epoch'], cfg)} ({ok})")
    nr = state["next_reset_epoch"]
    np = state["next_prime_epoch"]
    lines.append(f"⏳ Next reset: <b>{fmt(nr, cfg)}</b> (in {(nr-now)/3600:.1f} h)")
    lines.append(f"🤖 Next prime: {fmt(np, cfg)} (in {(np-now)/3600:.1f} h)")
    if state.get("plan_end"):
        try:
            ps = datetime.fromisoformat(state["plan_start"])
            pe = datetime.fromisoformat(state["plan_end"])
            f = "%Y-%m-%d %H:%M"
            lines.append(f"📅 Plan: {ps.strftime(f)} → {pe.strftime(f)} "
                         "(window resets at end)")
        except Exception:  # noqa: BLE001 - display only
            pass
    if paused:
        lines.append("⏸ <b>Paused</b> - auto-prime is off (/resume to enable)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the actual prime
# --------------------------------------------------------------------------- #
def run_prime(cfg: dict):
    cmd = ["claude", "-p", cfg["prompt"], "--model", cfg["model"],
           "--output-format", "json"]
    start = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=cfg.get("claude_timeout_secs", 120))
    except subprocess.TimeoutExpired:
        return False, "claude timed out"
    except FileNotFoundError:
        return False, "claude CLI not found in PATH"
    dur = time.time() - start
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        return False, f"exit={proc.returncode}: {err}"
    try:
        out = json.loads(proc.stdout)
        reply = str(out.get("result", "")).strip()[:60]
        return True, f"reply='{reply}' ({dur:.1f}s)"
    except json.JSONDecodeError:
        return True, f"ok ({dur:.1f}s)"


def do_prime(cfg: dict, state: dict, *, reason: str) -> None:
    now = time.time()
    cycle = cfg["cycle_minutes"] * 60
    margin = cfg["margin_minutes"] * 60

    ok, detail = run_prime(cfg)

    next_reset = now + cycle
    next_prime = next_reset + margin
    state.update({
        "last_prime_epoch": now,
        "last_prime_ok": ok,
        "last_prime_detail": detail,
        "next_reset_epoch": next_reset,
        "next_prime_epoch": next_prime,
    })
    # A prime firing means the plan's target window is now live (or the user
    # overrode it manually) - drop the one-shot plan marker either way.
    state.pop("plan_start", None)
    state.pop("plan_end", None)
    save_state(state)

    if ok:
        log(f"PRIME OK ({reason}): {detail}")
        if cfg.get("notify_on_prime"):
            tg_send(cfg,
                    "✅ <b>Limits primed</b>\n"
                    f"🕐 Now: {fmt(now, cfg)}\n"
                    "♻️ New 5-hour window is active\n"
                    f"⏳ Resets: <b>{fmt(next_reset, cfg)}</b>\n"
                    f"🤖 Next prime: {fmt(next_prime, cfg)}\n"
                    "🔗 Chain anchored here (the only active schedule)")
    else:
        log(f"PRIME FAIL ({reason}): {detail}")
        if cfg.get("notify_on_failure"):
            tg_send(cfg,
                    "⚠️ <b>Failed to prime limits</b>\n"
                    f"🕐 {fmt(now, cfg)}\n❌ {detail}\n"
                    f"🔁 Retry: {fmt(next_prime, cfg)}")


def tick_once(cfg: dict, state: dict) -> None:
    if not state.get("next_prime_epoch") or state.get("paused"):
        return
    if time.time() >= state["next_prime_epoch"]:
        do_prime(cfg, state, reason="scheduled")


# --------------------------------------------------------------------------- #
# telegram bot (long polling) + scheduler
# --------------------------------------------------------------------------- #
HELP = (
    "🤖 <b>claude-limit-primer</b>\n"
    "Keeps your Claude Code limits running and notifies you.\n\n"
    "<b>Model:</b> there is always exactly ONE schedule. Your last command is "
    "the single source of truth and fully replaces the previous one. After each "
    "prime the chain continues every 5h automatically.\n\n"
    "<b>Commands:</b>\n"
    "/prime - limits reset now: prime &amp; start the chain from now\n"
    "/reset - same as /prime (a quick \"it just reset\" button)\n"
    "/init HH:MM [Zone] - schedule the first prime at a clock time\n"
    "   e.g. <code>/init 02:00 Europe/Moscow</code>\n"
    "/plan START END [Zone] - reset the window at the END of a work session\n"
    "   e.g. <code>/plan 08:00 10:00</code> resets at 10:00 (smart reset)\n"
    "/reset HH:MM - change that clock time\n"
    "/status - current window and next prime\n"
    "/pause - pause auto-priming\n"
    "/resume - resume\n"
    "/cycle N - window length in minutes (default 300)\n"
    "/margin N - minutes after reset to prime (default 3)\n"
    "/tz Zone - timezone (e.g. Europe/Moscow)\n"
    "/help - this help"
)


# Shown in Telegram's "/" command menu (registered via setMyCommands on start).
BOT_COMMANDS = [
    ("prime", "Limits reset now: prime & chain from now"),
    ("reset", "Same as /prime, or /reset HH:MM for a clock time"),
    ("init", "Schedule first prime at a clock time, e.g. 02:00 Europe/Moscow"),
    ("plan", "Reset window at end of session, e.g. /plan 08:00 10:00"),
    ("status", "Current window and next prime"),
    ("pause", "Pause auto-priming"),
    ("resume", "Resume auto-priming"),
    ("cycle", "Window length in minutes (default 300)"),
    ("margin", "Minutes after reset to prime (default 3)"),
    ("tz", "Set timezone, e.g. Europe/Moscow"),
    ("help", "Show help"),
]


def register_commands(cfg: dict) -> None:
    """Register the command menu so they appear under '/' in Telegram."""
    token = cfg.get("telegram_token", "")
    if not token:
        return
    cmds = [{"command": c, "description": d} for c, d in BOT_COMMANDS]
    try:
        res = tg_api(token, "setMyCommands", {"commands": json.dumps(cmds)}, timeout=20)
        log("bot: commands registered" if res.get("ok")
            else f"bot: setMyCommands ok=false: {res}")
    except Exception as e:  # noqa: BLE001
        log(f"setMyCommands failed: {e}")


def handle_command(cfg: dict, text: str, chat_id) -> None:
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]   # strip @botname
    args = parts[1:]

    def reply(msg):
        tg_send(cfg, msg, chat_id=chat_id)

    if cmd in ("/start", "/help"):
        reply(HELP)
        return

    if cmd == "/status":
        reply(status_text(cfg, load_state()))
        return

    if cmd == "/init":
        if not args:
            reply("Provide a time: <code>/init 02:00 [Europe/Moscow]</code>")
            return
        tz = args[1].strip("[]") if len(args) > 1 else None
        try:
            if tz:
                ZoneInfo(tz)  # validate
        except ZoneInfoNotFoundError:
            reply(f"Unknown timezone: {tz}. Example: Europe/Moscow")
            return
        try:
            state = set_anchor(load_config(), args[0], tz)
        except ValueError:
            reply("Time format is HH:MM, e.g. 02:00")
            return
        reply("✅ Schedule set (replaces any previous - one schedule only).\n"
              + status_text(load_config(), state))
        return

    if cmd == "/plan":
        if len(args) < 2:
            reply("Plan a work session: <code>/plan START END [Zone]</code>\n"
                  "e.g. <code>/plan 08:00 10:00</code>\n"
                  "The window resets right at END, so you are topped up when\n"
                  "you finish - not stuck stretching limits over 5 hours.")
            return
        tz = args[2].strip("[]") if len(args) > 2 else None
        if tz:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                reply(f"Unknown timezone: {tz}. Example: Europe/Moscow")
                return
        try:
            state = set_plan(load_config(), args[0], args[1], tz)
        except PlanError as e:
            reply("🚫 " + str(e))
            return
        except ValueError:
            reply("Use HH:MM with START before END, e.g. "
                  "<code>/plan 08:00 10:00</code>")
            return
        cfg2 = load_config()
        ps = datetime.fromisoformat(state["plan_start"])
        pe = datetime.fromisoformat(state["plan_end"])
        session_h = (pe - ps).total_seconds() / 3600
        msg = "✅ Plan set - the window resets at the <b>end</b> of your session.\n"
        if session_h > cfg2["cycle_minutes"] / 60 + 1e-6:
            msg += (f"⚠️ Session is {session_h:.1f}h (longer than the "
                    f"{cfg2['cycle_minutes']/60:.1f}h window): a reset will "
                    "happen mid-session.\n")
        reply(msg + status_text(cfg2, state))
        return

    if cmd in ("/reset", "/setreset"):
        # No time = "limits reset right now" -> prime now and chain from here.
        if not args:
            reply("⏳ Treating as: limits reset now. Priming...")
            do_prime(load_config(), load_state(), reason="reset-now")
            return
        try:
            state = set_anchor(load_config(), args[0])
        except ValueError:
            reply("Time format is HH:MM, e.g. 02:00 (or /reset with no time = reset now)")
            return
        reply("✅ Reset time updated (replaces any previous - one schedule only).\n"
              + status_text(load_config(), state))
        return

    if cmd == "/prime":
        reply("⏳ Priming limits...")
        do_prime(load_config(), load_state(), reason="manual")
        return

    if cmd == "/pause":
        state = load_state()
        state["paused"] = True
        save_state(state)
        reply("⏸ Auto-prime paused. /resume to enable.")
        return

    if cmd == "/resume":
        state = load_state()
        state["paused"] = False
        save_state(state)
        reply("▶️ Resumed.\n" + status_text(cfg, state))
        return

    if cmd in ("/cycle", "/margin"):
        if not args or not args[0].isdigit():
            reply(f"Provide minutes: <code>{cmd} 300</code>")
            return
        key = "cycle_minutes" if cmd == "/cycle" else "margin_minutes"
        cfg2 = load_config()
        cfg2[key] = int(args[0])
        save_config(cfg2)
        reply(f"✅ {key} = {args[0]} min. Re-run /reset HH:MM to apply to the schedule.")
        return

    if cmd == "/tz":
        if not args:
            reply("Provide a timezone: <code>/tz Europe/Moscow</code>")
            return
        tz = args[0].strip("[]")
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            reply(f"Unknown timezone: {tz}")
            return
        cfg2 = load_config()
        cfg2["tz"] = tz
        save_config(cfg2)
        reply(f"✅ Timezone: {tz}")
        return

    reply("Unknown command. /help for the list.")


def cmd_bot(args) -> None:
    cfg = load_config()
    token = cfg.get("telegram_token", "")
    if not token:
        print("ERROR: telegram_token is not set (use .env)", file=sys.stderr)
        sys.exit(1)

    log("bot: started")
    register_commands(cfg)
    if cfg.get("telegram_chat_id"):
        tg_send(cfg, "🟢 Primer started.\n" + status_text(cfg, load_state()))

    offset = None
    while True:
        # 1) scheduler - prime if due
        try:
            tick_once(load_config(), load_state())
        except Exception as e:  # noqa: BLE001
            log(f"tick error: {e}")

        # 2) poll telegram for commands (long poll)
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            res = tg_api(token, "getUpdates", params, timeout=35)
        except Exception as e:  # noqa: BLE001
            log(f"getUpdates error: {e}")
            time.sleep(5)
            continue

        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg or "text" not in msg:
                continue
            chat_id = msg["chat"]["id"]
            cfg = load_config()

            # auto-capture / authorize chat
            saved = str(cfg.get("telegram_chat_id", "")).strip()
            if not saved:
                cfg["telegram_chat_id"] = str(chat_id)
                save_config(cfg)
                log(f"bot: linked chat_id={chat_id}")
                tg_send(cfg, "🔗 Chat linked. " + HELP, chat_id=chat_id)
                continue
            if str(chat_id) != saved:
                log(f"bot: ignoring message from unauthorized chat {chat_id}")
                continue

            try:
                handle_command(cfg, msg["text"], chat_id)
            except Exception as e:  # noqa: BLE001
                log(f"handle error: {e}")
                tg_send(cfg, f"⚠️ Error: {e}", chat_id=chat_id)


# --------------------------------------------------------------------------- #
# CLI commands
# --------------------------------------------------------------------------- #
def _plain(txt: str) -> str:
    for tag in ("<b>", "</b>", "<code>", "</code>"):
        txt = txt.replace(tag, "")
    return txt


def cmd_init(args) -> None:
    cfg = load_config()
    state = set_anchor(cfg, args.reset, args.tz)
    print(_plain(status_text(load_config(), state)))
    if not cfg.get("telegram_token"):
        print("\nNote: Telegram not configured - set TELEGRAM_TOKEN in .env "
              "and run `primer.py bot`.")


def cmd_plan(args) -> None:
    cfg = load_config()
    try:
        state = set_plan(cfg, args.start, args.end, args.tz)
    except PlanError as e:
        print("Plan not set:", e)
        return
    except ValueError:
        print("Use HH:MM with START before END.")
        return
    ps = datetime.fromisoformat(state["plan_start"])
    pe = datetime.fromisoformat(state["plan_end"])
    session_h = (pe - ps).total_seconds() / 3600
    print(_plain(status_text(load_config(), state)))
    if session_h > cfg["cycle_minutes"] / 60 + 1e-6:
        print(f"\nNote: session is {session_h:.1f}h, longer than the "
              f"{cfg['cycle_minutes']/60:.1f}h window - a reset will happen "
              "mid-session.")


def cmd_tick(args) -> None:
    state = load_state()
    if "next_prime_epoch" not in state:
        log("tick: no state - run init first")
        return
    tick_once(load_config(), state)


def cmd_prime(args) -> None:
    do_prime(load_config(), load_state(), reason="manual")


def cmd_status(args) -> None:
    print(_plain(status_text(load_config(), load_state())))


def cmd_test_telegram(args) -> None:
    ok = tg_send(load_config(), "🔔 claude-limit-primer: test. Telegram works.")
    print("sent" if ok else "FAILED (check token/chat_id)")


def main() -> None:
    p = argparse.ArgumentParser(description="Claude Code limit primer")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bot", help="run Telegram bot + scheduler").set_defaults(func=cmd_bot)

    pi = sub.add_parser("init", help="set anchor reset time")
    pi.add_argument("--reset", required=True, help="reset time HH:MM (local)")
    pi.add_argument("--tz", help="timezone, e.g. Europe/Moscow")
    pi.set_defaults(func=cmd_init)

    pp = sub.add_parser("plan", help="reset window at end of a work session")
    pp.add_argument("--start", required=True, help="session start HH:MM (local)")
    pp.add_argument("--end", required=True, help="session end HH:MM (local)")
    pp.add_argument("--tz", help="timezone, e.g. Europe/Moscow")
    pp.set_defaults(func=cmd_plan)

    sub.add_parser("tick", help="prime if due").set_defaults(func=cmd_tick)
    sub.add_parser("prime", help="force a prime now").set_defaults(func=cmd_prime)
    sub.add_parser("status", help="show window state").set_defaults(func=cmd_status)
    sub.add_parser("test-telegram", help="send a test message").set_defaults(func=cmd_test_telegram)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
