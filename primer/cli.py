"""argparse CLI - same subcommands as the old single-file version.

Run modes:
  primer bot                          run the Telegram bot + scheduler (main)
  primer init --reset HH:MM --tz Z    set anchor from the CLI
  primer plan --end HH:MM             reset at a chosen time
  primer tick                         prime once if due (cron alternative)
  primer prime                        force a prime now
  primer status                       print window state
  primer test-telegram                send a test notification
"""

import argparse

from loguru import logger

from primer.bot import run_bot
from primer.logging_setup import setup_logging
from primer.notify import send_notification
from primer.prime import do_prime, tick_once
from primer.schedule import PlanError, set_anchor, set_plan, status_text
from primer.settings import load_config
from primer.state import load_state


def _plain(txt: str) -> str:
    for tag in ("<b>", "</b>", "<code>", "</code>"):
        txt = txt.replace(tag, "")
    return txt


def cmd_bot(_args: argparse.Namespace) -> None:
    run_bot()


def cmd_init(args: argparse.Namespace) -> None:
    cfg = load_config()
    state = set_anchor(cfg, args.reset, args.tz)
    print(_plain(status_text(load_config(), state)))
    if not cfg.telegram_token:
        print("\nNote: Telegram not configured - set TELEGRAM_TOKEN in .env and run `primer bot`.")


def cmd_plan(args: argparse.Namespace) -> None:
    try:
        state = set_plan(load_config(), args.end, args.tz)
    except PlanError as e:
        print("Plan not set:", e)
        return
    except ValueError:
        print("Time format is HH:MM.")
        return
    print(_plain(status_text(load_config(), state)))


def cmd_tick(_args: argparse.Namespace) -> None:
    state = load_state()
    if state.next_prime_epoch is None:
        logger.info("tick: no state - run init first")
        return
    tick_once(load_config(), state)


def cmd_prime(_args: argparse.Namespace) -> None:
    do_prime(load_config(), reason="manual")


def cmd_status(_args: argparse.Namespace) -> None:
    print(_plain(status_text(load_config(), load_state())))


def cmd_test_telegram(_args: argparse.Namespace) -> None:
    ok = send_notification(load_config(), "🔔 claude-limit-primer: test. Telegram works.")
    print("sent" if ok else "FAILED (check token/chat_id)")


def main() -> None:
    setup_logging()
    p = argparse.ArgumentParser(description="Claude Code limit primer")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bot", help="run Telegram bot + scheduler").set_defaults(func=cmd_bot)

    pi = sub.add_parser("init", help="set anchor reset time")
    pi.add_argument("--reset", required=True, help="reset time HH:MM (local)")
    pi.add_argument("--tz", help="timezone, e.g. Europe/Moscow")
    pi.set_defaults(func=cmd_init)

    pp = sub.add_parser("plan", help="reset window at a chosen time")
    pp.add_argument("--end", required=True, help="reset time HH:MM (local)")
    pp.add_argument("--tz", help="timezone, e.g. Europe/Moscow")
    pp.set_defaults(func=cmd_plan)

    sub.add_parser("tick", help="prime if due").set_defaults(func=cmd_tick)
    sub.add_parser("prime", help="force a prime now").set_defaults(func=cmd_prime)
    sub.add_parser("status", help="show window state").set_defaults(func=cmd_status)
    sub.add_parser("test-telegram", help="send a test message").set_defaults(func=cmd_test_telegram)

    args = p.parse_args()
    args.func(args)
