#!/usr/bin/env bash
# Cron alternative to the bot: ensure CLI tools are on PATH, then prime if due.
export PATH="$HOME/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/primer" tick
