#!/usr/bin/env bash
# console.nace.ai smoke.
#
# Default: connectivity + static shape only (no browser, no mailbox).
# Live n=1: NACE_SMOKE_LIVE=1 — real register, this-run 0600 json only.
# Never treat accounts.jsonl / pipeline.jsonl tail as success.
set -euo pipefail

PROVIDER_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$PROVIDER_DIR/../.." && pwd)"
cd "$ROOT"

unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy || true
export CPA_EXPORT_ENABLED=false
export CPA_REMOTE_INJECT=false
export CPA_PROBE_CHAT=false
export COUNT=1

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="${PYTHON:-python3}"
fi

echo "=== console reachability ==="
curl -sS --max-time 20 -o /dev/null -w "console=%{http_code} time=%{time_total}\n" \
  https://console.nace.ai/invite/adf5acm5 || {
  echo "console.nace.ai unreachable" >&2
  exit 1
}

echo "=== static checks (no browser) ==="
"$PY" - <<'PY'
from pathlib import Path

from providers.nace.protocol.constants import (
    DEFAULT_INVITE_URL,
    EXTENSION_DIR,
    KEY_RE,
    TURNSTILE_SITEKEY,
)

assert DEFAULT_INVITE_URL.endswith("/invite/adf5acm5"), DEFAULT_INVITE_URL
assert TURNSTILE_SITEKEY.startswith("0x"), TURNSTILE_SITEKEY
man = EXTENSION_DIR / "manifest.json"
assert man.is_file(), f"missing extension manifest: {man}"
assert (EXTENSION_DIR / "content.js").is_file(), "missing extension content.js"
assert '"manifest_version": 3' in man.read_text(encoding="utf-8"), "extension must be MV3"
import re
assert re.fullmatch(KEY_RE, "nace_sk_EXAMPLEshape0123456789abcdefghijklmnop")
assert not re.fullmatch(KEY_RE, "nace_sk_EXAMPLE••••")
print("static_ok", "ext=" + str(EXTENSION_DIR))
PY

if [[ "${NACE_SMOKE_LIVE:-0}" != "1" ]]; then
  echo "SMOKE_OK (static only; set NACE_SMOKE_LIVE=1 for n=1 live register)"
  exit 0
fi

echo "=== live n=1 (this-run api_key only) ==="
mkdir -p logs providers/nace/output
TS=$(date +%Y%m%d_%H%M%S)
LOG="logs/nace_smoke_${TS}.log"
BEFORE=$("$PY" - <<'PY'
from pathlib import Path
p = Path("providers/nace/output")
p.mkdir(parents=True, exist_ok=True)
print(max((f.stat().st_mtime for f in p.glob("nace-*.json")), default=0))
PY
)

set +e
./register.sh nace 1 >"$LOG" 2>&1
RC=$?
set -e
echo "register_rc=$RC log=$LOG"

"$PY" - "$RC" "$LOG" "$BEFORE" <<'PY'
import json, sys
from pathlib import Path

rc = int(sys.argv[1])
log = Path(sys.argv[2]).read_text(errors="replace")
before = float(sys.argv[3])
out = Path("providers/nace/output")
fresh = [p for p in out.glob("nace-*.json") if p.stat().st_mtime >= before - 1]

contract = None
head, sep, tail = log.rpartition("CONTRACT_EXIT:")
if sep:
    first = tail.strip().splitlines()[0].strip() if tail.strip() else ""
    try:
        contract = int(first)
    except ValueError:
        contract = None

summary = None
decoder = json.JSONDecoder()
scan, pos = head, 0
while pos < len(scan):
    brace = scan.find("{", pos)
    if brace < 0:
        break
    try:
        obj, end = decoder.raw_decode(scan, brace)
    except json.JSONDecodeError:
        pos = brace + 1
        continue
    pos = end
    if isinstance(obj, dict) and "results" in obj and "ok" in obj:
        summary = obj

ok = bool(summary and int(summary.get("ok") or 0) == 1)
kind = email = ""
if summary and summary.get("results"):
    r0 = summary["results"][0]
    kind = str(r0.get("secret_kind") or "")
    email = str(r0.get("email") or "")
auth = json.loads(fresh[-1].read_text(encoding="utf-8")) if fresh else None

live_ok = (
    rc == 0
    and contract == 0
    and ok
    and kind == "api_key"
    and auth is not None
    and bool(auth.get("ok"))
    and bool(str(auth.get("api_key") or "").strip())
    and (not email or auth.get("email") == email)
)
print(json.dumps({
    "live_ok": live_ok,
    "rc": rc,
    "contract_exit": contract,
    "secret_kind": kind,
    "email": email,
    "fresh_json": str(fresh[-1]) if fresh else "",
    "ledgers_ignored": True,
}, ensure_ascii=False))
if not live_ok:
    sys.exit(1)
PY

echo SMOKE_OK
