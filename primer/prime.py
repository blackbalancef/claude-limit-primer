"""The actual prime: a tiny `claude -p` request + chain scheduling."""

import json
import subprocess
import time

from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from primer.notify import send_notification
from primer.schedule import fmt, h
from primer.settings import Config
from primer.state import State, load_state, save_state


class PrimeAttemptError(Exception):
    """A single `claude -p` attempt failed in a retryable way (timeout / nonzero exit)."""


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type(PrimeAttemptError),
)
def _prime_attempt(cfg: Config) -> tuple[str, float]:
    """One `claude -p` call. Returns (success detail, attempt start epoch) or raises.

    The start epoch matters: the usage window opens when the request is
    received, so the chain must anchor at the moment the SUCCESSFUL attempt
    started - not at do_prime entry, which retries can leave minutes behind.
    """
    cmd = ["claude", "-p", cfg.prompt, "--model", cfg.model, "--output-format", "json"]
    start = time.time()
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=cfg.claude_timeout_secs,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PrimeAttemptError("claude timed out") from exc
    dur = time.time() - start
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        raise PrimeAttemptError(f"exit={proc.returncode}: {err}")
    try:
        out = json.loads(proc.stdout)
        reply = str(out.get("result", "")).strip()[:60]
    except json.JSONDecodeError:
        return f"ok ({dur:.1f}s)", start
    return f"reply='{reply}' ({dur:.1f}s)", start


def run_prime(cfg: Config) -> tuple[bool, str, float]:
    """Prime once (with quick tenacity retries on transient failures).

    Returns (ok, detail, anchor_epoch): on success the anchor is when the
    successful attempt started; on failure, the moment the last attempt gave up.
    """
    try:
        detail, started = _prime_attempt(cfg)
    except FileNotFoundError:
        return False, "claude CLI not found in PATH", time.time()
    except PrimeAttemptError as exc:
        return False, str(exc), time.time()
    return True, detail, started


def do_prime(cfg: Config, *, reason: str) -> None:
    cycle = cfg.cycle_minutes * 60
    margin = cfg.margin_minutes * 60

    ok, detail, now = run_prime(cfg)

    # Re-load AFTER the (possibly multi-minute) claude call: a bot command may
    # have saved state meanwhile - only overwrite the fields this prime owns,
    # never write back a stale pre-prime snapshot.
    state: State = load_state()
    state.last_prime_epoch = now
    state.last_prime_ok = ok
    state.last_prime_detail = detail
    # A prime attempt consumes/overrides any one-shot plan marker. On failure
    # the exact planned reset is missed, so we retry soon instead of pretending
    # a full new window was opened.
    state.plan_start_epoch = None
    state.plan_end = None
    state.plan_end_epoch = None
    state.pop_extra("plan_start")

    if ok:
        next_reset: float | None = now + cycle
        next_prime = now + cycle + margin
        state.next_reset_epoch = next_reset
        state.next_prime_epoch = next_prime
        state.retry_after_failure = False
        state.retry_reason = None
    else:
        retry_minutes = max(1, cfg.failure_retry_minutes)
        next_reset = None
        next_prime = now + retry_minutes * 60
        state.next_prime_epoch = next_prime
        state.retry_after_failure = True
        state.retry_reason = detail
    save_state(state)

    if ok:
        logger.info(f"PRIME OK ({reason}): {detail}")
        if cfg.notify_on_prime:
            send_notification(
                cfg,
                "✅ <b>Limits primed</b>\n"
                f"🕐 Now: {fmt(now, cfg)}\n"
                f"♻️ New {cfg.cycle_minutes / 60:.1f}-hour window is active\n"
                f"⏳ Resets: <b>{fmt(next_reset, cfg)}</b>\n"
                f"🤖 Next prime: {fmt(next_prime, cfg)}\n"
                "🔗 Chain anchored here (the only active schedule)",
            )
    else:
        logger.error(f"PRIME FAIL ({reason}): {detail}")
        if cfg.notify_on_failure:
            send_notification(
                cfg,
                "⚠️ <b>Failed to prime limits</b>\n"
                f"🕐 {fmt(now, cfg)}\n❌ {h(detail)}\n"
                f"🔁 Retry: {fmt(next_prime, cfg)}",
            )


def tick_once(cfg: Config, state: State) -> None:
    if not state.next_prime_epoch or state.paused:
        return
    if time.time() >= state.next_prime_epoch:
        do_prime(cfg, reason="scheduled")
