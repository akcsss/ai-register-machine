#!/usr/bin/env python3
"""Offline unit tests for the console.nace.ai provider (no live register)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from register_core.contracts import Mailbox, OtpCode, RegisterResult
from register_core.errors import FailFastError, MailMissError
from register_core.providers.nace_adapter import NaceProvider
from register_core.providers.registry import get_provider, list_providers
from register_core.verify.nace_key import NaceKeyVerifier
from register_core.verify.registry import get_verifier

LIVE_KEY = "nace_sk_EXAMPLEshape0123456789abcdefghijklmnop"
MASKED_KEY = "nace_sk_mEn6" + "\u2022" * 20


class FakeEmail:
    name = "fake"

    def __init__(self, *, fail_otp: bool = False, code: str = "183826") -> None:
        self.fail_otp = fail_otp
        self.code = code
        self.released: list[tuple[str, bool, bool]] = []
        self.poll_kwargs: list[dict[str, Any]] = []
        self.allocated = 0
        self._lock = threading.Lock()

    def allocate(self) -> Mailbox:
        with self._lock:
            self.allocated += 1
        return Mailbox(
            address="newuser@hotmail.com",
            token="t",
            provider=self.name,
            meta={"local": "newuser", "domain": "hotmail.com"},
        )

    def poll_otp(self, mailbox: Mailbox, **kwargs: Any) -> OtpCode:
        with self._lock:
            self.poll_kwargs.append(dict(kwargs))
        if self.fail_otp:
            raise MailMissError("otp timeout test")
        return OtpCode(code=self.code, source=self.name)

    def release(
        self, mailbox: Mailbox, *, success: bool, consumed: bool = False
    ) -> None:
        with self._lock:
            self.released.append((mailbox.address, success, consumed))


class TestNaceRegistry(unittest.TestCase):
    def test_registered_and_aliases(self):
        self.assertIn("nace", list_providers())
        self.assertEqual(get_provider("nace").name, "nace")
        self.assertEqual(get_provider("drex").name, "nace")
        self.assertEqual(get_provider("nace.ai").name, "nace")
        self.assertEqual(get_provider("nace-ai").name, "nace")
        v = get_verifier("nace")
        self.assertEqual(v.name, "nace_key")
        self.assertEqual(get_verifier("drex").name, "nace_key")

    def test_default_mailbox_is_real_inbox(self):
        # CF temp-mail drops nace OTP → the default must be a real inbox.
        self.assertEqual(NaceProvider().email_source_name, "hotmail")


class TestNaceVerifier(unittest.TestCase):
    def test_shape_gate(self):
        v = NaceKeyVerifier(live=False)
        self.assertFalse(v.verify(RegisterResult(ok=True, provider="nace", secret="short")).ok)
        self.assertFalse(
            v.verify(
                RegisterResult(ok=True, provider="nace", secret=MASKED_KEY, secret_kind="api_key")
            ).ok
        )
        self.assertFalse(
            v.verify(
                RegisterResult(
                    ok=True, provider="nace", secret=LIVE_KEY, secret_kind="refresh_token"
                )
            ).ok
        )
        good = v.verify(
            RegisterResult(ok=True, provider="nace", secret=LIVE_KEY, secret_kind="api_key")
        )
        self.assertTrue(good.ok)
        self.assertIn("shape_ok", good.detail)
        self.assertFalse(v.verify(RegisterResult(ok=False, provider="nace", secret=LIVE_KEY)).ok)


class TestNaceKeyExtraction(unittest.TestCase):
    def test_full_keys_only_accepts_revealed(self):
        from providers.nace.protocol.flow import _full_keys

        raw = json.dumps([MASKED_KEY, LIVE_KEY, "not-a-key"])
        self.assertEqual(_full_keys(raw), [LIVE_KEY])
        self.assertEqual(_full_keys("[]"), [])
        self.assertEqual(_full_keys("garbage"), [])

    def test_bundled_extension_is_mv3(self):
        from providers.nace.protocol.constants import EXTENSION_DIR

        man = EXTENSION_DIR / "manifest.json"
        self.assertTrue(man.is_file())
        self.assertTrue((EXTENSION_DIR / "content.js").is_file())
        self.assertIn('"manifest_version": 3', man.read_text(encoding="utf-8"))


class TestNaceAdapterAttribution(unittest.TestCase):
    def test_success_from_this_run_only(self):
        email = FakeEmail()
        provider = NaceProvider(proxy="")
        from providers.nace.protocol.flow import NaceFlowResult

        fake_ok = NaceFlowResult(
            ok=True,
            email="newuser@hotmail.com",
            api_key="this-run-key-should-win",
            steps={"turnstile": {"ok": True}, "read_key": {"ok": True}},
        )
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            with patch("register_core.providers.nace_adapter.OUTPUT_DIR", out), patch(
                "providers.nace.protocol.flow.register_one", return_value=fake_ok
            ):
                ledger = out / "accounts.jsonl"
                ledger.write_text(
                    json.dumps(
                        {"ok": True, "email": "old@x.com", "api_key": "old-key-should-not-win"}
                    )
                    + "\n",
                    encoding="utf-8",
                )
                result = provider.register_one(email_source=email)
                auth_files = list(out.glob("nace-*.json"))
                self.assertEqual(len(auth_files), 1)
                written = json.loads(auth_files[0].read_text(encoding="utf-8"))
                self.assertEqual(written["api_key"], "this-run-key-should-win")
                # Historical ledger untouched; this-run json is the deliverable.
                self.assertNotIn("this-run-key-should-win", ledger.read_text(encoding="utf-8"))

        self.assertTrue(result.ok)
        self.assertEqual(result.secret, "this-run-key-should-win")
        self.assertEqual(result.secret_kind, "api_key")
        pub = result.to_public_dict()
        self.assertNotIn("this-run-key-should-win", json.dumps(pub))
        self.assertEqual(email.released, [("newuser@hotmail.com", True, False)])

    def test_otp_provider_uses_nace_sender_hint(self):
        email = FakeEmail()
        provider = NaceProvider(proxy="")
        captured: list[str] = []

        def fake_register_one(**kwargs: Any):
            fn = kwargs.get("otp_provider")
            assert callable(fn)
            captured.append(fn())
            from providers.nace.protocol.flow import NaceFlowResult

            return NaceFlowResult(ok=True, email="newuser@hotmail.com", api_key=LIVE_KEY)

        with tempfile.TemporaryDirectory() as td, patch(
            "register_core.providers.nace_adapter.OUTPUT_DIR", Path(td)
        ), patch("providers.nace.protocol.flow.register_one", side_effect=fake_register_one):
            result = provider.register_one(email_source=email)
        self.assertTrue(result.ok)
        self.assertEqual(captured, ["183826"])
        self.assertEqual(email.poll_kwargs[0].get("sender_hint"), "nace")

    def test_missing_api_key_is_failure(self):
        email = FakeEmail()
        provider = NaceProvider(proxy="")
        from providers.nace.protocol.flow import NaceFlowResult

        fake = NaceFlowResult(ok=True, email="newuser@hotmail.com", api_key="")
        with patch("providers.nace.protocol.flow.register_one", return_value=fake):
            result = provider.register_one(email_source=email)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "token")
        self.assertEqual(result.artifacts.get("fail_step"), "api_key")
        # The address was submitted to the site → retire it (consumed=True).
        self.assertEqual(email.released[-1][1], False)
        self.assertEqual(email.released[-1][2], True)

    def test_flow_failure_maps_kind_and_keeps_email(self):
        email = FakeEmail()
        provider = NaceProvider(proxy="")
        from providers.nace.protocol.flow import NaceFlowResult

        fake = NaceFlowResult(
            ok=False,
            email="newuser@hotmail.com",
            error="turnstile token not obtained",
            error_kind="captcha",
            fail_step="turnstile",
            steps={"turnstile": {"ok": False}},
        )
        with patch("providers.nace.protocol.flow.register_one", return_value=fake):
            result = provider.register_one(email_source=email)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "captcha")
        self.assertEqual(result.email, "newuser@hotmail.com")
        self.assertEqual(result.artifacts.get("fail_step"), "turnstile")
        # Pre-submit flake → the mailbox stays pickable (consumed=False).
        self.assertEqual(email.released[-1][1], False)
        self.assertEqual(email.released[-1][2], False)

    def test_exit0_without_identity_is_failure(self):
        # ok=True from the flow but no key → never reported as success.
        email = FakeEmail()
        provider = NaceProvider(proxy="")
        from providers.nace.protocol.flow import NaceFlowResult

        with patch(
            "providers.nace.protocol.flow.register_one",
            return_value=NaceFlowResult(ok=True, email="newuser@hotmail.com", api_key=""),
        ):
            result = provider.register_one(email_source=email)
        self.assertFalse(result.ok)
        self.assertEqual(result.secret_kind, "none")
        self.assertEqual(result.secret, "")

    def test_empty_mailbox_is_fail_fast(self):
        class EmptyEmail(FakeEmail):
            def allocate(self) -> Mailbox:
                return Mailbox(address="", token="", provider=self.name)

        provider = NaceProvider(proxy="")
        with self.assertRaises(FailFastError):
            provider.register_one(email_source=EmptyEmail())

    def test_unexpected_flow_exception_is_provider_error(self):
        from register_core.errors import ProviderError

        email = FakeEmail()
        provider = NaceProvider(proxy="")
        with patch(
            "providers.nace.protocol.flow.register_one",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(ProviderError):
                provider.register_one(email_source=email)
        self.assertEqual(email.released[-1][1], False)


if __name__ == "__main__":
    unittest.main()
