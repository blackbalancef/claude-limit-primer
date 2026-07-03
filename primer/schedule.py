"""Scheduling core: anchor, plan (smart reset), status rendering."""

import html
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from primer.settings import Config, save_config
from primer.state import State, load_state, save_state


def tzinfo(cfg: Config) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.tz)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def fmt(epoch: float | None, cfg: Config) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, tzinfo(cfg)).strftime("%Y-%m-%d %H:%M %Z")


def h(value: object) -> str:
    """Escape dynamic text embedded into Telegram HTML messages."""
    return html.escape(str(value), quote=False)


def set_anchor(cfg: Config, reset_str: str, tz: str | None = None) -> State:
    """Set the anchor reset time-of-day and compute the next prime."""
    if tz:
        cfg.tz = tz
        save_config(cfg)
    tz_i = tzinfo(cfg)
    now = datetime.now(tz_i)
    hh, mm = map(int, reset_str.strip().split(":"))
    reset_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    margin = timedelta(minutes=cfg.margin_minutes)
    # First prime lands on the next occurrence of the given clock time.
    # After that, do_prime() chains every cycle from the actual prime moment.
    first_prime = reset_today + margin
    while first_prime <= now:
        first_prime += timedelta(days=1)

    state = load_state()
    state.anchor_reset = reset_today.isoformat()
    state.next_reset_epoch = (first_prime - margin).timestamp()
    state.next_prime_epoch = first_prime.timestamp()
    state.paused = False
    state.plan_start_epoch = None
    state.plan_end = None
    state.plan_end_epoch = None
    state.retry_after_failure = None
    state.retry_reason = None
    state.pop_extra("plan_start")
    save_state(state)
    return state


class PlanError(ValueError):
    """Raised when a /plan target cannot be reached safely.

    The plan's prime would fire into a still-live window and fail to reset it.
    """


def set_plan(cfg: Config, end_str: str, tz: str | None = None) -> State:
    """Schedule a prime so the limit window RESETS exactly at END.

    The bot primes at END - cycle, opening the window [END - cycle, END], which
    is fresh from END - cycle and expires (resets to full) right at END - so you
    get topped up at the moment you chose, instead of waiting for the chain's
    next boundary.
    """
    if tz:
        cfg.tz = tz
        save_config(cfg)
    tz_i = tzinfo(cfg)
    now = datetime.now(tz_i)
    eh, em = map(int, end_str.strip().split(":"))
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)

    cycle = timedelta(minutes=cfg.cycle_minutes)
    margin = timedelta(minutes=cfg.margin_minutes)

    # Open the window with a prime at END - cycle so it expires exactly
    # at END. Roll forward a day at a time until that prime is in the future
    # (a prime can only be scheduled, never back-dated).
    target_prime = end - cycle
    while target_prime <= now:
        end += timedelta(days=1)
        target_prime += timedelta(days=1)

    # GUARD: a request can only START (or reset) a window if the previous one
    # has already expired. The plan's prime lands at target_prime (= END -
    # cycle); if the currently-live window is still alive then, the prime would
    # merely spend it (a tiny "pong") and reset nothing - so the boundary would
    # miss END entirely. Refuse rather than silently set a broken plan.
    state = load_state()
    cur_reset_epoch = state.next_reset_epoch
    if cur_reset_epoch:
        cur_reset = datetime.fromtimestamp(cur_reset_epoch, tz_i)
        if cur_reset > now and cur_reset + margin > target_prime:
            earliest = cur_reset + cycle + margin
            raise PlanError(
                f"Can't reset at {end:%H:%M}: the current window is still live "
                f"until {cur_reset:%b %d %H:%M}, but the plan needs to open a "
                f"fresh window at {target_prime:%H:%M} (= END - "
                f"{cfg.cycle_minutes // 60}h) - earlier than that expiry. "
                f"Plan no earlier than {earliest:%b %d %H:%M}, or wait until "
                f"after {cur_reset:%H:%M} and set the plan again."
            )

    state.plan_start_epoch = target_prime.timestamp()
    state.plan_end = end.isoformat()  # kept for backward-readable state
    state.plan_end_epoch = end.timestamp()
    state.next_reset_epoch = (target_prime - margin).timestamp()
    state.next_prime_epoch = target_prime.timestamp()
    state.paused = False
    state.anchor_reset = None
    state.retry_after_failure = None
    state.retry_reason = None
    save_state(state)
    return state


def fmt_dur(seconds: float) -> str:
    """Human-readable countdown, e.g. '1 hour 5 minutes' or '5 minutes'."""
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return "less than a minute"
    total_min = int(seconds // 60)
    hours, mins = divmod(total_min, 60)

    def pl(n: int, word: str) -> str:
        return f"{n} {word}{'s' if n != 1 else ''}"

    if hours and mins:
        return f"{pl(hours, 'hour')} {pl(mins, 'minute')}"
    if hours:
        return pl(hours, "hour")
    return pl(mins, "minute")


def _plan_end_epoch(state: State) -> float | None:
    if state.plan_end_epoch:
        return state.plan_end_epoch
    if not state.plan_end:
        return None
    try:
        return datetime.fromisoformat(state.plan_end).timestamp()
    except ValueError:  # display only
        return None


def status_text(cfg: Config, state: State) -> str:
    np = state.next_prime_epoch
    if not np:
        return (
            "No anchor set. Use <code>/init HH:MM [Zone]</code>\n"
            "e.g. <code>/init 02:00 Europe/Moscow</code>"
        )
    now = time.time()
    lines = [
        "📊 <b>Primer status</b>",
        f"🌍 Timezone: {h(cfg.tz)}",
        f"🕐 Now: {fmt(now, cfg)}",
        f"♻️ Cycle: every {cfg.cycle_minutes / 60:.1f} h "
        f"(prime {cfg.margin_minutes} min after reset)",
    ]
    if state.last_prime_epoch:
        ok = "OK" if state.last_prime_ok else "FAIL"
        line = f"✔️ Last prime: {fmt(state.last_prime_epoch, cfg)} ({ok})"
        if not state.last_prime_ok and state.last_prime_detail:
            line += f": {h(state.last_prime_detail)}"
        lines.append(line)

    pe = _plan_end_epoch(state)
    if state.retry_after_failure:
        lines.append("⚠️ Retrying after a failed prime")
        lines.append(f"🔁 Retry prime: <b>{fmt(np, cfg)}</b> (in {fmt_dur(np - now)})")
        lines.append("⏳ Next reset: will be known after a successful prime")
    elif pe:
        ps = state.plan_start_epoch or np
        lines.append(f"📅 Planned reset: <b>{fmt(pe, cfg)}</b>")
        lines.append(f"🤖 Planned prime: {fmt(ps, cfg)} (in {fmt_dur(ps - now)})")
        lines.append(f"🪟 Planned window: {fmt(ps, cfg)} → {fmt(pe, cfg)}")
    else:
        nr = state.next_reset_epoch
        if nr:
            lines.append(f"⏳ Next reset: <b>{fmt(nr, cfg)}</b> (in {fmt_dur(nr - now)})")
        else:
            lines.append("⏳ Next reset: -")
        lines.append(f"🤖 Next prime: {fmt(np, cfg)} (in {fmt_dur(np - now)})")

    if state.paused:
        lines.append("⏸ <b>Paused</b> - auto-prime is off (/resume to enable)")
    return "\n".join(lines)
