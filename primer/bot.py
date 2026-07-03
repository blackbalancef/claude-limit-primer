"""aiogram 3 Telegram bot + background scheduler tick.

One process does two jobs: a background asyncio task runs the scheduler tick
(prime if due) every ~25 s, while aiogram long-polls Telegram for commands.
"""

import asyncio
import contextlib
import sys
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, Message
from loguru import logger

from primer.notify import send_notification
from primer.prime import do_prime, tick_once
from primer.schedule import PlanError, h, set_anchor, set_plan, status_text
from primer.settings import Config, load_config, save_config
from primer.state import load_state, save_state

HELP = (
    "🤖 <b>claude-limit-primer</b>\n"
    "Keeps your Claude Code limits running and notifies you.\n\n"
    "<b>Model:</b> there is always exactly ONE schedule. Your last command is "
    "the single source of truth and fully replaces the previous one. After each "
    "prime the chain continues every 5h automatically.\n\n"
    "<b>Commands:</b>\n"
    "/prime - limits reset now: prime &amp; start the chain from now\n"
    '/reset - same as /prime (a quick "it just reset" button)\n'
    "/init HH:MM [Zone] - schedule the first prime at a clock time\n"
    "   e.g. <code>/init 02:00 Europe/Moscow</code>\n"
    "/plan END [Zone] - reset the window at a chosen time (smart reset)\n"
    "   e.g. <code>/plan 10:00</code>\n"
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
    ("plan", "Reset window at a chosen time, e.g. /plan 10:00"),
    ("status", "Current window and next prime"),
    ("pause", "Pause auto-priming"),
    ("resume", "Resume auto-priming"),
    ("cycle", "Window length in minutes (default 300)"),
    ("margin", "Minutes after reset to prime (default 3)"),
    ("tz", "Set timezone, e.g. Europe/Moscow"),
    ("help", "Show help"),
]

TICK_INTERVAL_SECS = 25

router = Router()

# do_prime runs in a worker thread; the lock keeps the scheduler tick,
# chat-triggered primes AND every state-writing command from overlapping
# (the old code was single-threaded, so handlers could never interleave with
# an in-flight prime - aiogram dispatches each update as its own task).
_prime_lock = asyncio.Lock()

# The in-flight prime/tick worker, if any. Threads cannot be cancelled, so
# shutdown awaits this instead of abandoning a prime before its save_state.
_inflight: asyncio.Task[None] | None = None


async def _locked_worker(fn: Callable[[], None]) -> None:
    """Run a prime/tick in a worker thread under the lock, tracked for shutdown."""
    global _inflight  # noqa: PLW0603 - single-process bot, shutdown needs the handle
    async with _prime_lock:
        task = asyncio.ensure_future(asyncio.to_thread(fn))
        _inflight = task
        try:
            # shield + re-await: cancelling the caller (e.g. scheduler shutdown)
            # must not abandon the worker thread mid-prime.
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        finally:
            if task.done():  # else keep the handle so shutdown can still await it
                _inflight = None


async def _prime(reason: str) -> None:
    await _locked_worker(lambda: do_prime(load_config(), reason=reason))


async def handle_command(cfg: Config, message: Message, text: str) -> None:
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]  # strip @botname
    args = parts[1:]

    async def reply(msg: str) -> None:
        await message.answer(msg)

    async def ack(msg: str) -> None:
        """Best-effort pre-action reply: a Telegram blip must never cancel a prime."""
        try:
            await message.answer(msg)
        except Exception as e:  # noqa: BLE001 - notification failures must not block priming
            logger.error(f"ack send failed: {e}")

    if cmd in ("/start", "/help"):
        await reply(HELP)
        return

    if cmd == "/status":
        await reply(status_text(cfg, load_state()))
        return

    if cmd == "/init":
        if not args:
            await reply("Provide a time: <code>/init 02:00 [Europe/Moscow]</code>")
            return
        tz = args[1].strip("[]") if len(args) > 1 else None
        try:
            if tz:
                ZoneInfo(tz)  # validate
        except ZoneInfoNotFoundError:
            await reply(f"Unknown timezone: {h(tz)}. Example: Europe/Moscow")
            return
        try:
            # under the lock: an in-flight prime must not clobber the new schedule
            async with _prime_lock:
                state = set_anchor(load_config(), args[0], tz)
        except ValueError:
            await reply("Time format is HH:MM, e.g. 02:00")
            return
        await reply(
            "✅ Schedule set (replaces any previous - one schedule only).\n"
            + status_text(load_config(), state)
        )
        return

    if cmd == "/plan":
        if not args:
            await reply(
                "Reset the window at a chosen time: <code>/plan END [Zone]</code>\n"
                "e.g. <code>/plan 10:00</code> or <code>/plan 10:00 Europe/Moscow</code>"
            )
            return
        # HH:MM always contains ":", a timezone never does -> split on that.
        times = [a for a in args if ":" in a]
        tzargs = [a for a in args if ":" not in a]
        if len(times) != 1 or len(tzargs) > 1:
            await reply("Usage: <code>/plan END [Zone]</code>\ne.g. <code>/plan 10:00</code>")
            return
        tz = tzargs[0].strip("[]") if tzargs else None
        if tz:
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                await reply(f"Unknown timezone: {h(tz)}. Example: Europe/Moscow")
                return
        try:
            # under the lock: an in-flight prime must not clobber the new plan
            async with _prime_lock:
                state = set_plan(load_config(), times[0], tz)
        except PlanError as e:
            await reply("🚫 " + h(e))
            return
        except ValueError:
            await reply("Time format is HH:MM, e.g. <code>/plan 10:00</code>")
            return
        cfg2 = load_config()
        plan_end = state.plan_end
        if plan_end is not None:  # always set by set_plan; narrows Optional
            pe = datetime.fromisoformat(plan_end)
            await reply(
                f"✅ Plan set - the window will reset at <b>{pe:%H:%M}</b> "
                f"on {pe:%Y-%m-%d}.\n" + status_text(cfg2, state)
            )
        return

    if cmd in ("/reset", "/setreset"):
        # No time = "limits reset right now" -> prime now and chain from here.
        if not args:
            await ack("⏳ Treating as: limits reset now. Priming...")
            await _prime("reset-now")
            return
        try:
            # under the lock: an in-flight prime must not clobber the new schedule
            async with _prime_lock:
                state = set_anchor(load_config(), args[0])
        except ValueError:
            await reply("Time format is HH:MM, e.g. 02:00 (or /reset with no time = reset now)")
            return
        await reply(
            "✅ Reset time updated (replaces any previous - one schedule only).\n"
            + status_text(load_config(), state)
        )
        return

    if cmd == "/prime":
        await ack("⏳ Priming limits...")
        await _prime("manual")
        return

    if cmd == "/pause":
        # under the lock: an in-flight prime's save_state must not revert this
        async with _prime_lock:
            state = load_state()
            state.paused = True
            save_state(state)
        await reply("⏸ Auto-prime paused. /resume to enable.")
        return

    if cmd == "/resume":
        async with _prime_lock:
            state = load_state()
            state.paused = False
            save_state(state)
        await reply("▶️ Resumed.\n" + status_text(cfg, state))
        return

    if cmd in ("/cycle", "/margin"):
        if not args or not args[0].isdigit():
            await reply(f"Provide minutes: <code>{cmd} 300</code>")
            return
        cfg2 = load_config()
        if cmd == "/cycle":
            key = "cycle_minutes"
            cfg2.cycle_minutes = int(args[0])
        else:
            key = "margin_minutes"
            cfg2.margin_minutes = int(args[0])
        save_config(cfg2)
        await reply(f"✅ {key} = {args[0]} min. Re-run /reset HH:MM to apply to the schedule.")
        return

    if cmd == "/tz":
        if not args:
            await reply("Provide a timezone: <code>/tz Europe/Moscow</code>")
            return
        tz = args[0].strip("[]")
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            await reply(f"Unknown timezone: {h(tz)}")
            return
        cfg2 = load_config()
        cfg2.tz = tz
        save_config(cfg2)
        await reply(f"✅ Timezone: {h(tz)}")
        return

    await reply("Unknown command. /help for the list.")


@router.message()
@router.edited_message()
async def on_message(message: Message) -> None:
    text = message.text
    if text is None:
        return
    chat_id = message.chat.id
    cfg = load_config()

    # auto-capture / authorize chat (single-chat lock)
    saved = str(cfg.telegram_chat_id).strip()
    if not saved:
        cfg.telegram_chat_id = str(chat_id)
        save_config(cfg)
        logger.info(f"bot: linked chat_id={chat_id}")
        await message.answer("🔗 Chat linked. " + HELP)
        return
    if str(chat_id) != saved:
        logger.info(f"bot: ignoring message from unauthorized chat {chat_id}")
        return

    try:
        await handle_command(cfg, message, text)
    except Exception as e:  # noqa: BLE001 - one bad command must not kill the bot
        logger.error(f"handle error: {e}")
        await message.answer(f"⚠️ Error: {h(e)}")


async def _scheduler() -> None:
    while True:
        try:
            await _locked_worker(lambda: tick_once(load_config(), load_state()))
        except Exception as e:  # noqa: BLE001 - the scheduler must survive any tick error
            logger.error(f"tick error: {e}")
        await asyncio.sleep(TICK_INTERVAL_SECS)


async def _register_commands(bot: Bot) -> None:
    """Register the command menu so they appear under '/' in Telegram."""
    commands = [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS]
    try:
        ok = await bot.set_my_commands(commands)
    except Exception as e:  # noqa: BLE001 - menu registration is best-effort
        logger.error(f"setMyCommands failed: {e}")
        return
    logger.info("bot: commands registered" if ok else "bot: setMyCommands ok=false")


async def _run(cfg: Config) -> None:
    bot = Bot(
        token=cfg.telegram_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("bot: started")
    await _register_commands(bot)
    if cfg.telegram_chat_id:
        await asyncio.to_thread(
            send_notification, cfg, "🟢 Primer started.\n" + status_text(cfg, load_state())
        )

    scheduler = asyncio.create_task(_scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler
        # A prime already talking to claude cannot be cancelled (worker thread);
        # wait for it so its save_state lands before the process exits.
        if _inflight is not None and not _inflight.done():
            logger.info("bot: waiting for in-flight prime to finish before shutdown")
            with contextlib.suppress(Exception):
                await _inflight


def run_bot() -> None:
    cfg = load_config()
    if not cfg.telegram_token:
        print("ERROR: telegram_token is not set (use .env)", file=sys.stderr)  # noqa: T201
        raise SystemExit(1)
    asyncio.run(_run(cfg))
