"""
Quick sanity checks for the AI provider configuration used by auto_classify.py.

Run inside your virtualenv from the mailbox_filtering directory:

    python test_providers.py

It will:
  - Load API keys from environment or data/api_keys.json
  - Try a minimal request to:
        - OpenAI (gpt-4o-mini)
        - Anthropic (claude-haiku-4-5-20251001)
  - Print either a short 'ok' style response or a clear error message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


BASE = Path(__file__).parent
API_KEYS_FILE = BASE / "data" / "api_keys.json"


def load_keys() -> dict:
    if API_KEYS_FILE.exists():
        try:
            return json.loads(API_KEYS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def test_openai(keys: dict) -> None:
    print("\n=== OpenAI test ===")
    try:
        from openai import OpenAI  # type: ignore
    except Exception as e:  # pragma: no cover
        print("OpenAI SDK import failed:", repr(e))
        return

    api_key = os.environ.get("OPENAI_API_KEY") or keys.get("OPENAI_API_KEY")
    print("API key present:", bool(api_key))
    if not api_key:
        print("SKIP: no OpenAI key configured.")
        return

    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Say 'ok'"}],
            max_tokens=5,
        )
        msg = resp.choices[0].message
        print("Success. Model replied:", msg)
    except Exception as e:
        print("OpenAI call failed:", repr(e))


def test_anthropic(keys: dict) -> None:
    print("\n=== Anthropic test ===")
    try:
        from anthropic import Anthropic  # type: ignore
    except Exception as e:  # pragma: no cover
        print("anthropic import failed:", repr(e))
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY") or keys.get("ANTHROPIC_API_KEY")
    print("API key present:", bool(api_key))
    if not api_key:
        print("SKIP: no ANTHROPIC_API_KEY configured.")
        return

    try:
        client = Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": "Say 'ok'"}],
        )
        print("Success. Model replied:", resp.content)
    except Exception as e:
        print("Anthropic call failed:", repr(e))


def main() -> None:
    print("Using API keys from:", API_KEYS_FILE)
    keys = load_keys()
    test_openai(keys)
    test_anthropic(keys)


if __name__ == "__main__":
    main()

