"""console.nace.ai register result verifier — API key shape gate (no live spend)."""

from __future__ import annotations

import re
from typing import Any

from register_core.contracts import RegisterResult, VerifyResult

_KEY_RE = re.compile(r"^nace_sk_[A-Za-z0-9_\-]{16,}$")


class NaceKeyVerifier:
    """Validate this-run nace API key presence and shape offline."""

    name = "nace_key"

    def __init__(self, *, live: bool = False, **_: Any) -> None:
        self.live = live

    def verify(self, result: RegisterResult) -> VerifyResult:
        if not result.ok:
            return VerifyResult(
                ok=False,
                provider="nace",
                capability="api_key",
                detail="register not ok",
            )
        secret = (result.secret or "").strip()
        kind = (result.secret_kind or "").strip().lower()
        if kind and kind not in ("api_key", "token"):
            return VerifyResult(
                ok=False,
                provider="nace",
                capability="api_key",
                detail=f"unexpected secret_kind={kind}",
            )
        if not _KEY_RE.match(secret):
            return VerifyResult(
                ok=False,
                provider="nace",
                capability="api_key",
                detail=f"api_key shape invalid len={len(secret)}",
            )
        return VerifyResult(
            ok=True,
            provider="nace",
            capability="api_key",
            detail="shape_ok (live probe off)" if not self.live else "shape_ok",
            meta={
                "secret_len": len(secret),
                "secret_kind": kind or "api_key",
                "live": bool(self.live),
            },
        )
