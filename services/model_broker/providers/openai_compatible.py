from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

# Transient upstream failures (5xx under load, dropped connections, read
# timeouts) are retried with exponential backoff plus jitter so a provider blip
# doesn't surface as a failed conversation. Jitter avoids synchronized retries
# when several agents hit the same blip at once.
_RETRY_STATUSES = {500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BACKOFF_SEC = 0.6


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = str(api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _urlopen_json(req: urllib.request.Request, timeout_sec: float) -> dict:
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in _RETRY_STATUSES or attempt == _MAX_ATTEMPTS - 1:
                raise
        except OSError as exc:
            # OSError is the common base of urllib.error.URLError (connect-phase
            # failures, DNS), socket.timeout / TimeoutError (read-phase timeouts
            # while awaiting or reading the response), and ConnectionError /
            # ConnectionResetError (mid-response drops). HTTPError is handled
            # above, so this catches exactly the transient transport failures the
            # module docstring promises to retry — including the read timeouts
            # that a plain URLError clause would miss.
            last_exc = exc
            if attempt == _MAX_ATTEMPTS - 1:
                raise
        time.sleep(_BACKOFF_SEC * (2 ** attempt) + random.uniform(0, 0.3))
    if last_exc:
        raise last_exc
    raise RuntimeError("request failed with no response")


def _post_json(url: str, payload: dict, api_key: str, timeout_sec: float) -> dict:
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(api_key),
    )
    return _urlopen_json(req, timeout_sec)


def _get_json(url: str, api_key: str, timeout_sec: float) -> dict:
    req = urllib.request.Request(url, method="GET", headers=_headers(api_key))
    return _urlopen_json(req, timeout_sec)


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
