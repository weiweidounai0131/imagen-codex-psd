import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ocr_credentials  # noqa: E402


class CredentialSafetyTests(unittest.TestCase):
    def test_status_reports_source_without_printing_value(self):
        secret = "test-token-never-print"
        output = io.StringIO()
        with patch.object(ocr_credentials, "resolve_credential",
                          return_value=ocr_credentials.Credential(secret, "environment:PADDLEOCR_ACCESS_TOKEN")):
            with contextlib.redirect_stdout(output):
                result = ocr_credentials.main(["status"])
        self.assertEqual(result, 0)
        self.assertIn("configured=yes", output.getvalue())
        self.assertIn("source=environment:PADDLEOCR_ACCESS_TOKEN", output.getvalue())
        self.assertNotIn(secret, output.getvalue())

    def test_official_environment_name_is_preferred(self):
        with patch.dict(os.environ, {"PADDLEOCR_ACCESS_TOKEN": "official-token",
                                    "PADDLE_OCR_TOKEN": "legacy-token"}, clear=True), \
             patch.object(ocr_credentials, "_keychain_token", return_value=""), \
             patch.object(ocr_credentials, "_legacy_config_token", return_value=""):
            resolved = ocr_credentials.resolve_credential()
        self.assertEqual(resolved.source, "environment:PADDLEOCR_ACCESS_TOKEN")
        self.assertEqual(resolved.value, "official-token")

    def test_keychain_setup_uses_prompt_flag_without_secret_argument(self):
        with patch.object(ocr_credentials.sys, "platform", "darwin"), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(ocr_credentials.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(ocr_credentials.configure_keychain(), 0)
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "-w")
        self.assertNotIn("official-token", command)

    def test_missing_credential_is_reported_without_fallback_claim(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(ocr_credentials, "_keychain_token", return_value=""), \
             patch.object(ocr_credentials, "_legacy_config_token", return_value=""):
            resolved = ocr_credentials.resolve_credential()
        self.assertEqual(resolved.value, "")
        self.assertEqual(resolved.source, "not-configured")


if __name__ == "__main__":
    unittest.main()
