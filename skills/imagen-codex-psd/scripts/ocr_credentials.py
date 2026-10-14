#!/usr/bin/env python3
"""Resolve PaddleOCR credentials without displaying or logging their values."""
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

SERVICE = "com.openai.imagen-codex-psd.paddleocr"


class Credential(NamedTuple):
    value: str
    source: str


def _keychain_token() -> str:
    if sys.platform != "darwin" or not Path("/usr/bin/security").is_file():
        return ""
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(), "-s", SERVICE, "-w"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _legacy_config_token() -> str:
    config_home = Path(os.environ.get("EDITPPT_CONFIG_HOME", "~/.editppt")).expanduser()
    path = config_home / "config.yaml"
    if not path.is_file():
        return ""
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("PADDLE_OCR_TOKEN", "")).strip()


def resolve_credential() -> Credential:
    for name in ("PADDLEOCR_ACCESS_TOKEN", "PADDLE_OCR_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return Credential(value, f"environment:{name}")
    token = _keychain_token()
    if token:
        return Credential(token, "macos-keychain")
    token = _legacy_config_token()
    if token:
        return Credential(token, "legacy-editppt-config")
    return Credential("", "not-configured")


def configure_keychain() -> int:
    if sys.platform != "darwin" or not Path("/usr/bin/security").is_file():
        print("PaddleOCR Keychain setup is available on macOS only.", file=sys.stderr)
        return 2
    print("macOS Keychain will prompt for the access token; the value is not passed as a command argument.")
    try:
        subprocess.run(
            ["/usr/bin/security", "add-generic-password", "-U", "-a", getpass.getuser(), "-s", SERVICE, "-w"],
            check=True,
        )
    except subprocess.CalledProcessError:
        print("Keychain did not save the credential.", file=sys.stderr)
        return 2
    print("Credential configured in macOS Keychain. OCR service connectivity has not been tested.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "configure"))
    args = parser.parse_args(argv)
    if args.action == "configure":
        return configure_keychain()
    credential = resolve_credential()
    print(f"configured={'yes' if credential.value else 'no'} source={credential.source}; live_service_validation=not_tested")
    return 0 if credential.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
