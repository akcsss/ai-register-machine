"""DrissionPage Chromium session for the nace.ai browser flow.

Turnstile clears only with a *headed* real Chromium plus the bundled MV3
extension. Playwright's default CDP profile is the detection trigger, so this
provider drives DrissionPage directly (see docs/ADDING_PROVIDER.md style:
in-process, owns its browser stack).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from providers.nace.protocol.constants import resolve_extension_dir


class NaceBrowserError(RuntimeError):
    """Raised when the browser stack cannot be brought up."""


def _candidate_chrome_paths() -> list[str]:
    """Ordered Chromium candidates: env pin, ms-playwright, then DrissionPage."""
    cands: list[str] = []
    env = (os.environ.get("NACE_CHROME_PATH") or "").strip()
    if env:
        cands.append(env)

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        base = Path(local) / "ms-playwright"
        exe = "chrome.exe"
    else:
        base = Path.home() / ".cache" / "ms-playwright"
        exe = "chrome"

    if base.is_dir():
        # Prefer the build proven to clear Turnstile (Chrome 149 / chromium-1228),
        # then any other installed build.
        dirs = sorted(
            (d for d in base.glob("chromium-*") if d.is_dir()),
            key=lambda d: (d.name != "chromium-1228", d.name),
        )
        for d in dirs:
            for sub in ("chrome-win64", "chrome-linux", "chrome-linux64", "chrome-mac"):
                p = d / sub / exe
                if p.is_file():
                    cands.append(str(p))
                    break
    return cands


def build_options(*, chrome_path: str | None = None, proxy: str | None = None):
    """Build ChromiumOptions for the Turnstile-clearing stack."""
    try:
        from DrissionPage import ChromiumOptions
    except Exception as exc:  # pragma: no cover - import guard
        raise NaceBrowserError(f"DrissionPage unavailable: {exc}") from exc

    co = ChromiumOptions()
    path = (chrome_path or "").strip()
    if not path:
        for cand in _candidate_chrome_paths():
            if cand and Path(cand).is_file():
                path = cand
                break
    if path:
        co.set_browser_path(path)

    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--remote-allow-origins=*")

    ext = resolve_extension_dir()
    if ext and Path(ext).is_dir():
        co.add_extension(ext)

    if proxy:
        try:
            co.set_proxy(proxy)
        except Exception:
            co.set_argument(f"--proxy-server={proxy}")

    co.auto_port()
    co.set_timeouts(base=2)
    return co


def open_browser(*, chrome_path: str | None = None, proxy: str | None = None):
    """Launch a Chromium and return (browser, first_tab)."""
    from DrissionPage import Chromium

    co = build_options(chrome_path=chrome_path, proxy=proxy)
    try:
        b = Chromium(co)
    except Exception as exc:  # pragma: no cover - launch guard
        raise NaceBrowserError(f"Chromium launch failed: {exc}") from exc
    tab = b.new_tab()
    return b, tab
