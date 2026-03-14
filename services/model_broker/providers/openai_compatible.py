from __future__ import annotations

import json
import urllib.request


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = str(api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _post_json(url: str, payload: dict, api_key: str, timeout_sec: float) -> dict:
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(api_key),
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, api_key: str, timeout_sec: float) -> dict:
    req = urllib.request.Request(url, method="GET", headers=_headers(api_key))
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_connection(*, base_url: str, model: str, api_key: str, timeout_sec: float) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply in JSON with keys reply and mode."},
            {"role": "user", "content": "ping"},
        ],
        "temperature": 0,
        "max_tokens": 32,
    }
    return _post_json(base_url.rstrip("/") + "/chat/completions", payload, api_key, timeout_sec)


def list_models(*, base_url: str, api_key: str, timeout_sec: float) -> dict:
    return _get_json(base_url.rstrip("/") + "/models", api_key, timeout_sec)


def chat_respond(*, base_url: str, api_key: str, payload: dict, timeout_sec: float) -> dict:
    return _post_json(base_url.rstrip("/") + "/chat/completions", payload, api_key, timeout_sec)
