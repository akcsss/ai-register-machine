"""console.nace.ai provider — in-process DrissionPage browser register."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from register_core.contracts import RegisterResult, normalize_error_kind
from register_core.email.base import EmailSource
from register_core.email.mail_proxy import resolve_mail_proxy
from register_core.email.registry import get_email_source
from register_core.errors import FailFastError, MailMissError, ProviderError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROTOCOL_DIR = ROOT / "providers" / "nace"
OUTPUT_DIR = PROTOCOL_DIR / "output"

__all__ = ["NaceProvider", "resolve_mail_proxy"]

# Providers whose mailbox must be a real inbox (Cloudflare Email Routing drops
# nace.ai OTP for the project temp domains; Hotmail/Outlook delivery works).
_REAL_INBOX_SOURCES = frozenset({"hotmail", "outlook", "gmail", "gmail_imap"})


def _redact_proxy(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return "(none)"
    try:
        if "@" in s:
            return s.split("@", 1)[-1]
    except Exception:
        pass
    return s[:80]


def _save_artifact(email: str, api_key: str) -> Path | None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        OUTPUT_DIR.chmod(0o700)
    except Exception:
        pass
    safe_email = (email or "unknown").replace("@", "_at_").replace("/", "_")
    out_path = OUTPUT_DIR / f"nace-{safe_email}-{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    payload = {
        "ok": True,
        "provider": "nace",
        "email": email,
        "api_key": api_key,
        "secret": api_key,
        "secret_kind": "api_key",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api_base": "https://console.nace.ai/v1",
        "model": "drex-v1.5",
    }
    # 0600: this-run deliverable, one key per file.
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception:
        return None
    return out_path


def _was_submitted(steps: Any, fail_step: str) -> bool:
    """True once the email reached the site (account may exist).

    Pre-submit steps (open_invite/turnstile/browser) are transient flakes — the
    address stays pickable. Any later failure (otp/verify_code/read_key/api_key)
    means the address was already submitted, so it must be retired to keep a
    serial batch from looping on the same mailbox.
    """
    pre_submit = {"open_invite", "turnstile", "browser", "unexpected"}
    if isinstance(steps, dict) and steps.get("submit_email", {}).get("ok"):
        return True
    fs = str(fail_step or "").strip()
    return bool(fs) and fs not in pre_submit


class NaceProvider:
    """In-process console.nace.ai register via DrissionPage + Turnstile patch.

    Consumes an EmailSource (default ``hotmail`` — a real inbox is required).
    Produces an API key as the primary secret. Never injects production CPA.
    Serial only: Pipeline keeps nace off the parallel provider set.
    """

    name = "nace"

    def __init__(
        self,
        *,
        proxy: str | None = None,
        email_source_name: str | None = None,
        otp_timeout_s: float = 240,
        invite_url: str | None = None,
        chrome_path: str | None = None,
        **_: Any,
    ) -> None:
        self.proxy = (
            proxy
            or os.environ.get("NACE_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or ""
        )
        self.email_source_name = (
            email_source_name
            or os.environ.get("NACE_EMAIL_SOURCE")
            or "hotmail"
        )
        self.otp_timeout_s = float(
            os.environ.get("NACE_OTP_TIMEOUT") or otp_timeout_s
        )
        self.invite_url = (
            invite_url or os.environ.get("NACE_INVITE_URL") or ""
        ).strip()
        self.chrome_path = (
            chrome_path or os.environ.get("NACE_CHROME_PATH") or ""
        ).strip()

    def register_one(
        self,
        *,
        email_source: EmailSource | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RegisterResult:
        from providers.nace.protocol import flow as nace_flow

        extra = dict(extra or {})
        proxy = str(extra.get("proxy") or self.proxy or "").strip()
        mail_proxy = resolve_mail_proxy(extra)
        otp_timeout = float(extra.get("otp_timeout_s") or self.otp_timeout_s)
        invite_url = str(extra.get("invite_url") or self.invite_url or "").strip() or None
        chrome_path = str(extra.get("chrome_path") or self.chrome_path or "").strip() or None
        keep_browser = bool(extra.get("keep_browser"))

        source = email_source
        if source is None:
            try:
                kw: dict[str, Any] = {"proxy": mail_proxy or None}
                source = get_email_source(self.email_source_name, **kw)
            except Exception as exc:
                raise FailFastError(f"nace email source unavailable: {exc}") from exc

        logs: list[str] = []

        def log(msg: str) -> None:
            line = str(msg)
            logs.append(line)
            print(f"[nace] {line}", flush=True)

        arts: dict[str, Any] = {
            "runtime": str(PROTOCOL_DIR),
            "email_source": getattr(source, "name", self.email_source_name),
            "proxy": _redact_proxy(proxy),
            "mail_proxy": _redact_proxy(mail_proxy) if mail_proxy else "(direct)",
            "note": "in-process console.nace.ai browser register; no cpa inject",
            "protocol": "nace_turnstile_otp",
            "invite_url": invite_url or "(default)",
        }

        mailbox = source.allocate()
        email = (mailbox.address or "").strip()
        if not email or "@" not in email:
            raise FailFastError("nace allocate returned empty mailbox")
        arts["mailbox_provider"] = mailbox.provider
        arts["email"] = email

        t0 = time.time()
        used_codes: set[str] = set()

        def otp_provider() -> str:
            otp = source.poll_otp(
                mailbox,
                timeout_s=otp_timeout,
                poll_interval_s=2.5,
                used_codes=used_codes or None,
                newer_than_epoch=t0 - 10,
                sender_hint="nace",
            )
            code = str(getattr(otp, "code", "") or "").strip()
            if code:
                used_codes.add(code)
            return code

        try:
            res = nace_flow.register_one(
                email=email,
                otp_provider=otp_provider,
                invite_url=invite_url,
                proxy=proxy or None,
                chrome_path=chrome_path,
                otp_timeout_s=otp_timeout,
                log=log,
                keep_browser=keep_browser,
            )
        except FailFastError:
            raise
        except Exception as exc:
            try:
                source.release(mailbox, success=False)
            except Exception:
                pass
            raise ProviderError(f"nace unexpected: {exc}") from exc

        if isinstance(res.steps, dict) and res.steps:
            arts["steps"] = res.steps
            arts["step_keys"] = sorted(str(k) for k in res.steps.keys())
        if res.fail_step:
            arts["fail_step"] = res.fail_step

        if not res.ok:
            try:
                source.release(
                    mailbox,
                    success=False,
                    consumed=_was_submitted(res.steps, res.fail_step),
                )
            except Exception:
                pass
            return RegisterResult(
                ok=False,
                provider=self.name,
                email=email,
                error=res.error or "register_failed",
                error_kind=normalize_error_kind(res.error_kind or "provider"),
                secret_kind="none",
                artifacts={**arts, "tail": "\n".join(logs)[-1500:]},
            )

        api_key = (res.api_key or "").strip()
        if not api_key:
            # res.ok with no key: email is definitely submitted and onboarded.
            try:
                source.release(mailbox, success=False, consumed=True)
            except Exception:
                pass
            arts["fail_step"] = arts.get("fail_step") or "api_key"
            return RegisterResult(
                ok=False,
                provider=self.name,
                email=email,
                error="missing_api_key",
                error_kind="token",
                secret_kind="none",
                artifacts={**arts, "tail": "\n".join(logs)[-1500:]},
            )

        out_path = _save_artifact(email, api_key)
        if out_path is not None:
            arts["auth_path"] = str(out_path)
        else:
            arts["auth_write_error"] = "artifact_write_failed"

        # Historical ledgers are not the success contract: do not append the
        # full key anywhere else; this-run 0600 json + pipeline sink own it.

        try:
            source.release(mailbox, success=True)
        except Exception:
            pass

        return RegisterResult(
            ok=True,
            provider=self.name,
            email=email,
            secret=api_key,
            secret_kind="api_key",
            artifacts={**arts, "tail": "\n".join(logs)[-1500:]},
        )
