#!/usr/bin/env bash
# console.nace.ai register runner (in-process browser via register_core).
# Usage:
#   ./providers/nace/run-register.sh [count]
#
# Requires a real inbox (Hotmail/Outlook) and a browser-capable host with a
# display or Xvfb. Serial only — the browser flow is heavy.
set -euo pipefail

COUNT="${COUNT:-${1:-1}}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

export REGISTER_EGRESS="${REGISTER_EGRESS:-${EGRESS_BACKEND:-${NACE_EGRESS:-}}}"
export NACE_PROXY="${NACE_PROXY:-${PROXY:-}}"
# Cloudflare temp-mail drops nace.ai OTP → default to the Hotmail pool.
export NACE_EMAIL_SOURCE="${NACE_EMAIL_SOURCE:-hotmail}"
export NACE_INVITE_URL="${NACE_INVITE_URL:-}"
export NACE_OTP_TIMEOUT="${NACE_OTP_TIMEOUT:-240}"
export NACE_CHROME_PATH="${NACE_CHROME_PATH:-}"
export REGISTER_NODES_FILE="${REGISTER_NODES_FILE:-${NODES_FILE:-$ROOT/nodes.json}}"

SINK="${NACE_SINK:-$ROOT/providers/nace/output/pipeline.jsonl}"
mkdir -p "$(dirname "$SINK")"

echo "[nace] COUNT=$COUNT egress=${REGISTER_EGRESS:-direct} proxy=${NACE_PROXY:-'(direct)'} email_source=$NACE_EMAIL_SOURCE" >&2

ARGS=(
  -m register_core run
  -p nace
  -n "$COUNT"
  --email-source "${NACE_EMAIL_SOURCE}"
  --sink "$SINK"
  --threads 1
  --timeout "${NACE_TIMEOUT:-1200}"
)
if [[ -n "${REGISTER_EGRESS}" ]]; then
  ARGS+=(--egress "${REGISTER_EGRESS}")
fi
if [[ -n "${NACE_PROXY}" ]]; then
  ARGS+=(--proxy "${NACE_PROXY}")
fi

exec "$PY" "${ARGS[@]}"
