#!/usr/bin/env python3
"""Standalone Groq key diagnostic.

This intentionally does not import analysis_engine.llm_client. It reads GROQ1
through GROQ5 directly from .env and calls Groq's OpenAI-compatible HTTP API
with requests so key health can be separated from application wiring.
"""

from __future__ import annotations

from pathlib import Path
import json
import os
import re
from typing import Iterable

import requests


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
API_URL = "https://api.groq.com/openai/v1/chat/completions"
PRIMARY_MODEL = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama-3.1-8b-instant"


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            values[name] = value
    return values


def _masked(value: str) -> str:
    if len(value) <= 10:
        return "<too-short>"
    return f"{value[:6]}...{value[-4:]}"


def _chat_completion(key: str, model: str) -> requests.Response:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Return a JSON object exactly like {\"status\":\"ok\"}.",
            }
        ],
        "temperature": 0,
        "max_tokens": 16,
        "response_format": {"type": "json_object"},
    }
    return requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )


def _print_response(label: str, model: str, response: requests.Response) -> None:
    print(f"{label} model={model} status={response.status_code}")
    print(response.text)


def _iter_key_names() -> Iterable[str]:
    for idx in range(1, 6):
        yield f"GROQ{idx}"


def main() -> int:
    env_values = _parse_env(ENV_PATH)
    print(f"Loaded env file: {ENV_PATH}")
    for name in _iter_key_names():
        key = env_values.get(name, os.getenv(name, "")).strip()
        if not key:
            print(f"{name}: missing")
            continue
        print(f"{name}: found {_masked(key)}")
        response = _chat_completion(key, PRIMARY_MODEL)
        _print_response(name, PRIMARY_MODEL, response)
        if response.status_code == 403:
            print(f"{name}: retrying fallback model after 403")
            fallback_response = _chat_completion(key, FALLBACK_MODEL)
            _print_response(name, FALLBACK_MODEL, fallback_response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
