#!/usr/bin/env bash

contract_make_tmpdir() {
  local root="$1"
  local name="$2"
  mkdir -p "$root/tmp"
  mktemp -d "$root/tmp/${name}.XXXXXX"
}

contract_make_runtime_root() {
  local root="$1"
  local name="$2"
  local runtime_root
  runtime_root="$(contract_make_tmpdir "$root" "$name")"
  mkdir -p "$runtime_root/control-plane/runtime"
  printf '%s\n' "$runtime_root"
}

contract_kill_pids() {
  local pid
  for pid in "$@"; do
    if [[ -n "${pid:-}" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
  for pid in "$@"; do
    if [[ -n "${pid:-}" ]]; then
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done
}
