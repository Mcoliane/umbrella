#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from pathlib import Path


GENESIS_HASH = 'GENESIS'


def _canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _hash_event(payload: dict) -> str:
    return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _state_path(run_dir: Path) -> Path:
    return run_dir / 'eventlog.state.json'


def _events_path(run_dir: Path) -> Path:
    return run_dir / 'events.jsonl'


def _load_state(run_dir: Path) -> dict:
    p = _state_path(run_dir)
    if not p.exists():
        return {'lastSeq': 0, 'lastHash': GENESIS_HASH}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return {
            'lastSeq': int(data.get('lastSeq', 0)),
            'lastHash': str(data.get('lastHash', GENESIS_HASH)),
        }
    except Exception:
        return {'lastSeq': 0, 'lastHash': GENESIS_HASH}


def _save_state(run_dir: Path, seq: int, event_hash: str):
    p = _state_path(run_dir)
    p.write_text(json.dumps({'lastSeq': seq, 'lastHash': event_hash}, indent=2) + '\n', encoding='utf-8')


def append_hashed_event(run_dir: Path, event: dict) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    state = _load_state(run_dir)

    seq = state['lastSeq'] + 1
    prev_hash = state['lastHash']

    core = dict(event)
    core.pop('eventHash', None)
    core.pop('prevHash', None)
    core.pop('seq', None)

    record = {
        'seq': seq,
        **core,
        'prevHash': prev_hash,
    }
    record['eventHash'] = _hash_event(record)

    log = _events_path(run_dir)
    with log.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record) + '\n')

    _save_state(run_dir, seq, record['eventHash'])
    return record


def verify_hashed_event_log(events_file: Path) -> dict:
    if not events_file.exists():
        return {'valid': False, 'count': 0, 'errors': [f'missing events file: {events_file}']}

    errors: list[str] = []
    prev_hash = GENESIS_HASH
    expected_seq = 1
    count = 0

    with events_file.open('r', encoding='utf-8') as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            count += 1
            try:
                obj = json.loads(line)
            except Exception as ex:
                errors.append(f'line {lineno}: invalid json: {ex}')
                continue

            seq = int(obj.get('seq', -1))
            if seq != expected_seq:
                errors.append(f'line {lineno}: seq mismatch expected={expected_seq} got={seq}')
            expected_seq += 1

            rec_prev = str(obj.get('prevHash', ''))
            if rec_prev != prev_hash:
                errors.append(f'line {lineno}: prevHash mismatch expected={prev_hash} got={rec_prev}')

            rec_hash = str(obj.get('eventHash', ''))
            check_obj = dict(obj)
            check_obj.pop('eventHash', None)
            computed = _hash_event(check_obj)
            if rec_hash != computed:
                errors.append(f'line {lineno}: eventHash mismatch')

            prev_hash = rec_hash

    return {
        'valid': len(errors) == 0,
        'count': count,
        'errors': errors,
        'lastHash': prev_hash,
    }
