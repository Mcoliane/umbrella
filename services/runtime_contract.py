from __future__ import annotations

import json
from fnmatch import fnmatchcase
from pathlib import Path


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class RuntimeContract:
    def __init__(self, path: Path):
        self.path = path
        self.config = _load_json(path, {})
        self.runtimes = self.config.get("runtimes") or {}

    def payload(self) -> dict:
        return {
            "loadedAtPath": str(self.path),
            "contract": self.config,
        }

    def supported_runtimes(self) -> list[str]:
        return list(self.runtimes.keys())

    def resolve_action(self, action_id: str) -> dict:
        original = str(action_id or "").strip()
        alias_runtime = ""
        resolved = original
        for runtime_id, runtime_cfg in self.runtimes.items():
            aliases = runtime_cfg.get("compatibilityAliases") or {}
            if original in aliases:
                alias_runtime = runtime_id
                resolved = str(aliases[original]).strip() or original
                break
        supported = self.supported_runtimes_for_action(resolved)
        preferred = supported[0] if supported else ""
        action_family = ""
        runtime_capability = ""
        for runtime_id in supported:
            family, capability = self.describe_action_for_runtime(runtime_id, resolved)
            if family or capability:
                action_family = family
                runtime_capability = capability
                break
        if not runtime_capability and alias_runtime and original != resolved:
            runtime_capability = f"runtime.alias.{original}"
        return {
            "originalActionId": original,
            "resolvedActionId": resolved,
            "deprecatedActionId": original if original != resolved else "",
            "aliasRuntime": alias_runtime,
            "supportedRuntimes": supported,
            "preferredRuntime": preferred,
            "actionFamily": action_family,
            "runtimeCapability": runtime_capability,
        }

    def supported_runtimes_for_action(self, action_id: str) -> list[str]:
        action = str(action_id or "").strip()
        supported = []
        for runtime_id in self.supported_runtimes():
            if self.runtime_supports_action(runtime_id, action):
                supported.append(runtime_id)
        return supported

    def runtime_supports_action(self, runtime_id: str, action_id: str) -> bool:
        runtime_cfg = self.runtimes.get(runtime_id) or {}
        action = str(action_id or "").strip()
        if not action:
            return False
        exact = runtime_cfg.get("exactActions") or []
        if action in exact:
            return True
        for family in runtime_cfg.get("actionFamilies") or []:
            if self._pattern_matches(family, action):
                return True
        for rule in runtime_cfg.get("capabilityRules") or []:
            if self._rule_matches(rule, action):
                return True
        return False

    def describe_action_for_runtime(self, runtime_id: str, action_id: str) -> tuple[str, str]:
        runtime_cfg = self.runtimes.get(runtime_id) or {}
        action = str(action_id or "").strip()
        for rule in runtime_cfg.get("capabilityRules") or []:
            if self._rule_matches(rule, action):
                return (
                    str(rule.get("actionFamily", "")).strip(),
                    str(rule.get("capability", "")).strip(),
                )
        if action in (runtime_cfg.get("exactActions") or []):
            capabilities = runtime_cfg.get("capabilities") or []
            return action, str(capabilities[0]).strip() if capabilities else ""
        for family in runtime_cfg.get("actionFamilies") or []:
            if self._pattern_matches(family, action):
                capabilities = runtime_cfg.get("capabilities") or []
                return family, str(capabilities[0]).strip() if capabilities else ""
        return "", ""

    def capability_alias_for(self, runtime_id: str, action_id: str) -> str:
        aliases = ((self.runtimes.get(runtime_id) or {}).get("compatibilityAliases") or {})
        return str(aliases.get(str(action_id or "").strip(), "")).strip()

    def resolve_compatible_runtime(self, action_id: str, requested_runtime: str, fallbacks: list[str] | None = None) -> tuple[str, bool, str]:
        action = str(action_id or "").strip()
        requested = str(requested_runtime or "").strip()
        supported = self.supported_runtimes_for_action(action)
        if not requested:
            return (supported[0] if supported else "", True, "implicit_runtime")
        if not supported or self.runtime_supports_action(requested, action):
            return requested, True, "requested_runtime"
        for fallback in fallbacks or []:
            if fallback in supported:
                return fallback, False, f"capability_reroute:{requested}->{fallback}"
        return (supported[0] if supported else ""), False, "requested_runtime_unsupported"

    @staticmethod
    def _pattern_matches(pattern: str, action: str) -> bool:
        if not pattern:
            return False
        if "*" in pattern or "?" in pattern or "[" in pattern:
            return fnmatchcase(action, pattern)
        return action == pattern

    def _rule_matches(self, rule: dict, action: str) -> bool:
        match_action = str(rule.get("matchAction", "")).strip()
        if match_action and action == match_action:
            return True
        match_pattern = str(rule.get("matchPattern", "")).strip()
        if match_pattern and self._pattern_matches(match_pattern, action):
            return True
        return False
