#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.runtime_model import broker_enabled, call_model_broker, load_model_broker


def _last_user_facts(history: list[dict]) -> str:
    for row in reversed(history):
        if not isinstance(row, dict):
            continue
        if str(row.get("role", "")).strip() != "user":
            continue
        content = str(row.get("content", "")).strip()
        if content:
            return content
    return ""


def _fallback(invocation: dict, inputs: dict) -> dict:
    message = str(inputs.get("message", "")).strip()
    agent_id = str(inputs.get("agentId", "")).strip() or "agent"
    runtime_mode = str(inputs.get("runtimeMode", "direct")).strip() or "direct"
    history = inputs.get("conversationHistory") if isinstance(inputs.get("conversationHistory"), list) else []
    available_shops = inputs.get("availableShops") if isinstance(inputs.get("availableShops"), list) else []
    lower = message.lower()
    worker_shops = [
        row for row in available_shops
        if isinstance(row, dict) and str(row.get("shopType", "")).strip() not in {"town-hall", "originator-studio"}
    ]

    if agent_id == "originator":
        if "create" in lower and "worker" in lower:
            return {
                "ok": True,
                "mode": "originate",
                "reply": "I can staff the town. Tell me which worker package you want to originate and what shop it should run.",
                "providerUsed": False,
                "modelUsed": "",
                "fallbackUsed": True,
            }
        return {
            "ok": True,
            "mode": "direct",
            "reply": "I manage town staffing and shops. Ask me to create a worker, assign a package, or explain the town roster.",
            "providerUsed": False,
            "modelUsed": "",
            "fallbackUsed": True,
        }

    if runtime_mode == "summarize":
        return {
            "ok": True,
            "mode": "direct",
            "reply": str(inputs.get("summary", "")).strip() or "The delegated work is complete.",
            "providerUsed": False,
            "modelUsed": "",
            "fallbackUsed": True,
        }

    if agent_id == "mayor":
        if any(token in lower for token in ["hello", "hi", "hey"]) and "?" not in message:
            return {"ok": True, "mode": "direct", "reply": "Hello. I’m the mayor. Tell me what you need and I’ll answer directly or coordinate the town.", "providerUsed": False, "modelUsed": "", "fallbackUsed": True}
        if "respond with" in lower:
            requested = message.split("respond with", 1)[1].strip().strip('"').strip("'")
            requested = requested.rstrip('?"\' ').strip()
            return {"ok": True, "mode": "direct", "reply": requested or "hello", "providerUsed": False, "modelUsed": "", "fallbackUsed": True}
        if any(token in lower for token in ["status", "town", "who are you", "what can you do"]):
            if worker_shops:
                shop_names = ", ".join(str(row.get("name", row.get("shopId", "shop"))).strip() for row in worker_shops)
                return {"ok": True, "mode": "direct", "reply": f"I oversee the town and can delegate work to {shop_names}.", "providerUsed": False, "modelUsed": "", "fallbackUsed": True}
            return {"ok": True, "mode": "direct", "reply": "I oversee the town hall. The originator can create worker shops when you need specialized help.", "providerUsed": False, "modelUsed": "", "fallbackUsed": True}
        if worker_shops and any(
            token in lower
            for token in ["fact:", "search", "look up", "find", "summarize", "research", "code", "program", "implement", "build", "write"]
        ):
            primary_shop = worker_shops[0]
            plan_action = str(primary_shop.get("preferredConversationAction", "")).strip() or "skill.chat.respond"
            return {
                "ok": True,
                "mode": "delegate",
                "reply": "",
                "providerUsed": False,
                "modelUsed": "",
                "fallbackUsed": True,
                "delegationPlan": [
                    {
                        "shopId": str(primary_shop.get("shopId", "")).strip(),
                        "actionId": plan_action,
                        "inputs": {
                            "message": message,
                            "agentId": str(primary_shop.get("ownerAgentId", "")).strip(),
                            "shopId": str(primary_shop.get("shopId", "")).strip(),
                            "runtimeMode": "direct",
                            "conversationHistory": history[-8:],
                        },
                    }
                ],
            }
        if not worker_shops:
            return {
                "ok": True,
                "mode": "direct",
                "reply": "I can answer basic questions directly, but there are no worker shops available yet. Ask the originator to create one when you need specialized help.",
                "providerUsed": False,
                "modelUsed": "",
                "fallbackUsed": True,
            }
        return {
            "ok": True,
            "mode": "direct",
            "reply": "I can answer directly or coordinate a worker shop. Tell me what outcome you want.",
            "providerUsed": False,
            "modelUsed": "",
            "fallbackUsed": True,
        }

    if any(token in lower for token in ["hello", "hi", "hey"]) and "?" not in message:
        return {"ok": True, "mode": "direct", "reply": f"Hello from {agent_id}.", "providerUsed": False, "modelUsed": "", "fallbackUsed": True}
    fact = ""
    if "fact:" in message:
        fact = message[message.find("fact:"):].split()[0]
    if fact:
        return {"ok": True, "mode": "direct", "reply": f"I found {fact} and I’m ready to keep working from there.", "providerUsed": False, "modelUsed": "", "fallbackUsed": True}
    previous = _last_user_facts(history)
    if previous and previous != message:
        return {"ok": True, "mode": "direct", "reply": f"{agent_id} understood. Current request: {message}. Previous context: {previous}.", "providerUsed": False, "modelUsed": "", "fallbackUsed": True}
    return {"ok": True, "mode": "direct", "reply": f"{agent_id} received: {message}", "providerUsed": False, "modelUsed": "", "fallbackUsed": True}


def _provider_response(inputs: dict) -> dict | None:
    broker = load_model_broker(ROOT)
    if not bool(broker.get("enabled", False)):
        return None
    override = inputs.get("modelProvider") if isinstance(inputs.get("modelProvider"), dict) else {}
    broker_request = {
        "connectionId": str(override.get("connectionId", "")).strip(),
        "agentPackageId": str(inputs.get("agentPackageId", "")).strip(),
        "agentPackageMetadata": inputs.get("agentPackageMetadata") if isinstance(inputs.get("agentPackageMetadata"), dict) else {},
        "agentId": str(inputs.get("agentId", "")).strip(),
        "shopId": str(inputs.get("shopId", "")).strip(),
        "message": str(inputs.get("message", "")).strip(),
        "conversationHistory": inputs.get("conversationHistory") if isinstance(inputs.get("conversationHistory"), list) else [],
        "townContext": inputs.get("townContext") if isinstance(inputs.get("townContext"), dict) else {},
        "availableShops": inputs.get("availableShops") if isinstance(inputs.get("availableShops"), list) else [],
        "subAgents": inputs.get("subAgents") if isinstance(inputs.get("subAgents"), list) else [],
        "runtimeMode": str(inputs.get("runtimeMode", "")).strip(),
        "systemPrompt": str(inputs.get("systemPrompt", "")).strip(),
        "instructions": str(inputs.get("instructions", "")).strip(),
        "model": str(override.get("model") or inputs.get("model", "")).strip(),
        "temperature": inputs.get("temperature"),
        "maxTokens": inputs.get("maxTokens"),
    }
    try:
        response = call_model_broker(ROOT, "/v1/chat/respond", broker_request, timeout_sec=120.0)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, json.JSONDecodeError) as exc:
        if broker_enabled(broker):
            return {
                "ok": True,
                "mode": "direct",
                "reply": f"The configured model backend is unavailable right now ({exc}). Run /model test or /model glm5.",
                "providerUsed": False,
                "modelUsed": "",
                "fallbackUsed": False,
                "providerError": True,
            }
        return None
    if not isinstance(response, dict):
        if broker_enabled(broker):
            return {
                "ok": True,
                "mode": "direct",
                "reply": "The configured model backend returned an invalid response. Run /model test or /model glm5.",
                "providerUsed": False,
                "modelUsed": "",
                "fallbackUsed": False,
                "providerError": True,
            }
        return None
    if not response.get("ok"):
        if broker_enabled(broker):
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            message = str(error.get("message", "")).strip() or "provider request failed"
            return {
                "ok": True,
                "mode": "direct",
                "reply": f"The configured model backend is unavailable right now ({message}). Run /model test or /model glm5.",
                "providerUsed": False,
                "modelUsed": "",
                "fallbackUsed": False,
                "providerError": True,
            }
        return None
    return response


def main() -> int:
    payload = json.load(sys.stdin)
    invocation = payload.get("invocation") if isinstance(payload.get("invocation"), dict) else {}
    inputs = invocation.get("inputs") if isinstance(invocation.get("inputs"), dict) else {}
    result = _provider_response(inputs) or _fallback(invocation, inputs)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
