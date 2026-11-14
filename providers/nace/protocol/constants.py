"""Static constants for the console.nace.ai registration flow.

The console is a Next.js (turbopack) app on Vercel. Auth is Supabase email OTP
(6-digit, no password) and the signup form is gated by a Cloudflare Turnstile
widget in invisible (PRIME CONTROL) mode. There are no REST routes: the form
POSTs a Next.js Server Action, so the flow is browser-driven.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- URLs -----------------------------------------------------------------
BASE_URL = "https://console.nace.ai"
DEFAULT_INVITE_URL = "https://console.nace.ai/invite/adf5acm5"
DASHBOARD_URL = "https://console.nace.ai/dashboard"
API_KEYS_URL = "https://console.nace.ai/dashboard/api-keys"

# OpenAI-ish compatible surface the issued key talks to.
API_BASE_URL = "https://console.nace.ai/v1"
DEFAULT_MODEL = "drex-v1.5"

# Cloudflare Turnstile sitekey used by the console signup form.
TURNSTILE_SITEKEY = "0x4AAAAAAFCRPOs3yZbksWF1"

# --- selectors ------------------------------------------------------------
TURNSTILE_RESPONSE_SELECTOR = "input[name=cf-turnstile-response]"
EMAIL_SELECTOR = "#email"
SUBMIT_SELECTOR = "css:button[type=submit]"
SHOW_NEW_KEY_TEXT = "show a new key"
SHOW_KEY_ARIA_LABEL = "Show key"
CREATE_KEY_TEXT = "Create key"
CREATE_KEY_INPUT_NAME = "name"
DEFAULT_KEY_NAME = "register-core"

# --- key shape ------------------------------------------------------------
# Live keys look like nace_sk_<40+ random url-safe chars>
KEY_RE = r"nace_sk_[A-Za-z0-9_\-]{16,}"

# --- local assets ---------------------------------------------------------
PROTOCOL_DIR = Path(__file__).resolve().parents[1]
EXTENSION_DIR = PROTOCOL_DIR / "extension"
OUTPUT_DIR = PROTOCOL_DIR / "output"


def resolve_extension_dir() -> str:
    """Path to the bundled MV3 Turnstile-patch extension (env override wins)."""
    override = os.environ.get("NACE_EXTENSION_DIR", "").strip()
    if override:
        return override
    return str(EXTENSION_DIR)
