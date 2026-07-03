"""claude-limit-primer.

Keeps your Claude Code subscription usage window *always ticking* by sending a
tiny request on a schedule, so the 5-hour limit clock starts at a time YOU
control instead of only when you manually send the first message of the day.
Everything is controllable from a Telegram bot - including re-adjusting the
anchor time on the fly if the schedule ever drifts.

Key timing rule: you can only RESTART the clock by priming AFTER the current
window has expired. So every prime is scheduled a few minutes *after* the
expected reset (margin_minutes), never before.
"""

__version__ = "2.0.0"
