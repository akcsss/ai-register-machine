"""console.nace.ai registration flow (invite → email OTP → API key).

Browser-driven because the signup form is a Next.js Server Action behind a
Cloudflare Turnstile widget; there is no REST surface to replay. The flow:

  1. open the invite URL (sets the ``drex_ref`` referral cookie)
  2. wait for the invisible Turnstile token to be solved
  3. submit the email → Supabase ``signInWithOtp``
  4. read the 6-digit code out-of-band (injected ``otp_provider`` callback)
  5. type the code → land on ``/dashboard`` with $25 credit
  6. reveal a fresh API key (``Show a new key`` → eye → read the ``<code>``)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from providers.nace.protocol import constants as C
from providers.nace.protocol.session import NaceBrowserError, open_browser


class OtpProvider(Protocol):
    """Blocking OTP fetch supplied by the adapter (mailbox poll)."""

    def __call__(self) -> str:  # pragma: no cover - protocol
        ...


class NaceRegisterError(RuntimeError):
    """One failed stage of the nace flow (carries step + kind for the adapter)."""

    def __init__(self, msg: str, *, step: str = "", kind: str = "provider") -> None:
        super().__init__(msg)
        self.step = step
        self.kind = kind
        self.steps: dict[str, Any] = {}


@dataclass
class NaceFlowResult:
    ok: bool
    email: str = ""
    api_key: str = ""
    error: str = ""
    error_kind: str = "provider"
    fail_step: str = ""
    steps: dict[str, Any] = field(default_factory=dict)


# --- JS helpers -----------------------------------------------------------
_JS_TURNSTILE_TOKEN = (
    "return (document.querySelector('%s')||{}).value||''" % C.TURNSTILE_RESPONSE_SELECTOR
)
_JS_FOCUS_CODE_INPUT = (
    "()=>{const ins=[...document.querySelectorAll('input')]"
    ".filter(e=>e.type!=='hidden'&&e.type!=='email'&&e.offsetParent!==null);"
    "if(ins.length)ins[0].focus();return ins.length;}"
)
_JS_CLICK_NEW_KEY = (
    "return (()=>{const bs=[...document.querySelectorAll('button')];"
    "const c=bs.find(b=>/%s/i.test(b.innerText||''));"
    "if(c){c.click();return 'clicked';}return 'notfound';})()" % C.SHOW_NEW_KEY_TEXT
)
_JS_CLICK_SHOW_KEY = (
    "return (()=>{const bs=[...document.querySelectorAll('button')];"
    "const c=bs.find(b=>(b.getAttribute('aria-label')||'')==='%s');"
    "if(c){c.click();return 'clicked';}return 'notfound';})()" % C.SHOW_KEY_ARIA_LABEL
)
_JS_READ_CODES = (
    "return (()=>{const out=[];"
    "document.querySelectorAll('code').forEach(e=>{const s=(e.textContent||'').trim();"
    "if(s)out.push(s);});"
    "document.querySelectorAll('input,textarea').forEach(e=>{const v=(e.value||'').trim();"
    "if(v)out.push(v);});"
    "const re=/%s/g;const h=document.documentElement.outerHTML;let m;"
    "while((m=re.exec(h)))out.push(m[0]);"
    "return JSON.stringify([...new Set(out)]);})()" % C.KEY_RE
)
# API-keys page: create a fresh key through the modal, which reveals the value
# in a readonly <input aria-label="New API key">.
_JS_HAS_CREATE = (
    "return !![...document.querySelectorAll('button')]"
    ".find(b=>/^create key$/i.test((b.innerText||'').trim()))"
)
_JS_CLICK_CREATE_PAGE = (
    "return (()=>{const bs=[...document.querySelectorAll('button')]"
    ".filter(b=>/^create key$/i.test((b.innerText||'').trim()));"
    "if(!bs.length)return 'notfound';bs[0].click();return 'clicked';})()"
)
_JS_FILL_KEY_NAME = (
    "return (()=>{const i=document.querySelector('input[name=name]')"
    "||document.querySelector('[role=dialog] input,[role=alertdialog] input');"
    "if(!i)return 'noinput';"
    "const set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
    "set.call(i,'%s');i.dispatchEvent(new Event('input',{bubbles:true}));return 'filled';})()"
    % C.DEFAULT_KEY_NAME
)
_JS_CLICK_CREATE_DIALOG = (
    "return (()=>{const sel=/^create key$/i;"
    "const dlg=[...document.querySelectorAll('button')].filter(b=>sel.test((b.innerText||'').trim())"
    "&&b.closest('[role=dialog],[role=alertdialog]'));"
    "if(dlg.length){dlg[dlg.length-1].click();return 'clicked_dialog';}"
    "const all=[...document.querySelectorAll('button')].filter(b=>sel.test((b.innerText||'').trim()));"
    "if(all.length>1){all[all.length-1].click();return 'clicked_last';}"
    "return 'notfound';})()"
)


def _full_keys(raw: Any) -> list[str]:
    """Filter a JSON code list down to fully-revealed keys (no mask dots)."""
    try:
        import json

        codes = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except Exception:
        return []
    out: list[str] = []
    for s in codes:
        s = str(s or "")
        if re.fullmatch(C.KEY_RE, s):
            out.append(s)
    return out


def register_one(
    *,
    email: str,
    otp_provider: OtpProvider,
    invite_url: str | None = None,
    proxy: str | None = None,
    chrome_path: str | None = None,
    turnstile_timeout_s: float = 45,
    otp_timeout_s: float = 240,
    post_submit_wait_s: float = 8,
    log: Callable[[str], None] | None = None,
    keep_browser: bool = False,
) -> NaceFlowResult:
    """Run one full nace registration and return the revealed API key."""
    logs: list[str] = []

    def _log(msg: str) -> None:
        line = str(msg)
        logs.append(line)
        if log:
            log(line)

    url = (invite_url or C.DEFAULT_INVITE_URL).strip()
    steps: dict[str, Any] = {}
    result = NaceFlowResult(ok=False, email=email, steps=steps)

    def _step(name: str, ok: bool = True, **info: Any) -> None:
        steps[name] = {"ok": bool(ok), **info}

    b = None
    try:
        b, t = open_browser(chrome_path=chrome_path, proxy=proxy)
        t.get(url)
        _step("open_invite", url=url)

        # 2. wait for the invisible Turnstile to mint a token
        token = ""
        deadline = time.time() + turnstile_timeout_s
        while time.time() < deadline:
            time.sleep(1.5)
            token = str(t.run_js(_JS_TURNSTILE_TOKEN) or "")
            if token:
                break
        _step("turnstile", bool(token), token_len=len(token))
        if not token:
            raise NaceRegisterError(
                "turnstile token not obtained", step="turnstile", kind="captcha"
            )

        # 3. email + submit
        t.ele(C.EMAIL_SELECTOR).clear().input(email)
        time.sleep(0.4)
        t.ele(C.SUBMIT_SELECTOR).click()
        _step("submit_email", email=email)
        time.sleep(post_submit_wait_s)

        # 4. OTP out-of-band
        try:
            code = str(otp_provider() or "").strip()
        except NaceRegisterError:
            raise
        except Exception as exc:
            raise NaceRegisterError(
                f"otp fetch failed: {exc}", step="otp", kind="mail_miss"
            ) from exc
        if not re.fullmatch(r"\d{4,8}", code):
            raise NaceRegisterError(
                f"otp invalid: {code!r}", step="otp", kind="mail_miss"
            )
        _step("otp", code_len=len(code))

        # 5. type the code
        t.run_js(_JS_FOCUS_CODE_INPUT)
        t.actions.type(code)
        time.sleep(8)
        t.url  # settle
        landed = "/dashboard" in (t.url or "")
        _step("verify_code", landed, url=t.url)
        if not landed:
            # give the SPA a little longer before giving up
            for _ in range(6):
                time.sleep(3)
                if "/dashboard" in (t.url or ""):
                    landed = True
                    break
            _step("verify_code", landed, url=t.url)
            if not landed:
                raise NaceRegisterError(
                    f"did not land on dashboard (url={t.url})",
                    step="verify_code",
                    kind="otp_invalid",
                )

        # 6. create + reveal a fresh API key on the API-keys page.
        # The dashboard landing has no key card once onboarded; keys live under
        # /dashboard/api-keys and are minted through a "Create key" modal that
        # reveals the value once in a readonly input.
        try:
            t.get(C.API_KEYS_URL)
        except Exception:
            pass
        time.sleep(3)
        for _ in range(10):
            if t.run_js(_JS_HAS_CREATE):
                break
            time.sleep(1.5)
        _step("api_keys_page", url=t.url)

        keys: list[str] = []
        clicked_page = t.run_js(_JS_CLICK_CREATE_PAGE)
        _step("open_create_dialog", clicked_page == "clicked", result=clicked_page)
        time.sleep(2)
        filled = t.run_js(_JS_FILL_KEY_NAME)
        _step("fill_key_name", filled in ("filled", "noinput"), result=filled)
        time.sleep(0.5)
        clicked_dialog = t.run_js(_JS_CLICK_CREATE_DIALOG)
        _step("confirm_create_key", clicked_dialog != "notfound", result=clicked_dialog)

        for attempt in range(8):
            time.sleep(2)
            keys = _full_keys(t.run_js(_JS_READ_CODES))
            if keys:
                break

        if not keys:
            # Fallback: legacy onboarding card (Show a new key → eye).
            clicked_new = t.run_js(_JS_CLICK_NEW_KEY)
            _step("reveal_click_new_key", clicked_new == "clicked", result=clicked_new)
            for attempt in range(3):
                time.sleep(3)
                t.run_js(_JS_CLICK_SHOW_KEY)
                time.sleep(2)
                keys = _full_keys(t.run_js(_JS_READ_CODES))
                if keys:
                    break

        _step("read_key", bool(keys), count=len(keys))
        if not keys:
            snippet = ""
            try:
                snippet = str(t.run_js(_JS_READ_CODES) or "")[:300]
            except Exception:
                pass
            # Debug aid: capture the real page state (gitignored output dir).
            try:
                dbg = C.OUTPUT_DIR / "nace_debug_readkey.html"
                C.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                dbg.write_text(t.html or "", encoding="utf-8")
                _step("read_key", False, count=0, debug_html=str(dbg), url=t.url)
            except Exception:
                pass
            raise NaceRegisterError(
                f"api key revealed but not captured (url={t.url} codes={snippet!r})",
                step="read_key",
                kind="token",
            )
        api_key = keys[0]

        result.ok = True
        result.api_key = api_key
        return result
    except NaceRegisterError as exc:
        exc.steps = steps
        result.error = str(exc)
        result.error_kind = exc.kind
        result.fail_step = exc.step
        _step("failed", False, step=exc.step, error=str(exc)[:200])
        return result
    except NaceBrowserError as exc:
        result.error = str(exc)
        result.error_kind = "fatal"
        result.fail_step = "browser"
        _step("failed", False, step="browser", error=str(exc)[:200])
        return result
    except Exception as exc:  # pragma: no cover - defensive
        result.error = f"unexpected: {exc}"
        result.error_kind = "provider"
        result.fail_step = "unexpected"
        _step("failed", False, step="unexpected", error=str(exc)[:200])
        return result
    finally:
        if b is not None and not keep_browser:
            try:
                b.quit()
            except Exception:
                pass