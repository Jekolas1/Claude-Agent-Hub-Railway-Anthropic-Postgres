#!/usr/bin/env python3
"""Upload the existing agents from ~/flai-work to the Claude Agent Hub on Railway.

Does not change anything in flai-work. Safe to re-run: every run refreshes the cloud copy
from the local files (instructions + text knowledge). Uses only the Python standard library.

Usage (on the Mac):  python3 ~/flai-work/sync_to_cloud.py
"""
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HUB = os.getenv('HUB_URL', 'https://claude-agent-hub-production.up.railway.app')
ROOT = Path(os.getenv('FLAI_WORK', Path.home() / 'flai-work'))

# Which agents to upload, and what each one may do in the cloud.
AGENTS = {
    'gershon': {'tools': ['code_execution'], 'shared_write': True, 'names': ['גרשון']},            # opens client files in clients/
    'gefen':   {'tools': ['web_search'], 'shared_write': True, 'names': ['גפן']},  # writes to shared/gefen/
    'tirosh':  {'tools': [], 'shared_write': False, 'names': ['תירוש']},           # reads gefen, writes only its own drafts
}
# Shared folders every agent can read.
SHARED_DIRS = ['clients', 'shared']
TEXT = {'.md', '.txt', '.csv'}
MAX_CHARS = 30000


def call(method, path, token, body=None):
    req = urllib.request.Request(
        HUB + path, method=method,
        data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f'\n✗ {method} {path} -> {e.code} {e.read().decode()[:300]}')


def text_files(folder, skip=()):
    for p in sorted(folder.rglob('*')):
        rel = p.relative_to(folder)
        if p.is_file() and p.suffix.lower() in TEXT and not any(part in skip for part in rel.parts):
            yield p


def upload(agent_id, path, token, skipped):
    key = path.relative_to(ROOT).as_posix()
    value = path.read_text(encoding='utf-8', errors='replace')
    if not value.strip():
        return
    if len(value) > MAX_CHARS:
        skipped.append(f'{key} (too long: {len(value)} chars)')
        return
    call('PUT', f'/agents/{agent_id}/knowledge/{urllib.parse.quote(key)}', token, {'value': value})
    print(f'   + {key}')


def main():
    if not ROOT.is_dir():
        sys.exit(f'Folder not found: {ROOT}')
    token = os.getenv('HUB_TOKEN') or getpass.getpass('HUB_TOKEN (hidden): ').strip()
    skipped = []

    for agent_id, cfg in AGENTS.items():
        folder = ROOT / 'Agents' / agent_id
        skill = folder / 'plugin' / 'skills' / agent_id / 'SKILL.md'
        if not skill.is_file():
            print(f'! {agent_id}: no SKILL.md, skipped')
            continue
        call('PUT', f'/agents/{agent_id}', token, {'instructions': skill.read_text(encoding='utf-8'), **cfg})
        print(f'✓ {agent_id} (tools={cfg["tools"]}, shared_write={cfg["shared_write"]})')
        for p in text_files(folder, skip={'plugin', '.claude-plugin'}):
            upload(agent_id, p, token, skipped)
        others = [p.relative_to(ROOT).as_posix() for p in folder.rglob('*')
                  if p.is_file() and p.suffix.lower() not in TEXT and p.name != '.DS_Store'
                  and 'plugin' not in p.relative_to(folder).parts]
        skipped += [f'{o} (binary, not uploaded yet)' for o in others]

    print('✓ shared')
    for d in SHARED_DIRS:
        if (ROOT / d).is_dir():
            for p in text_files(ROOT / d):
                upload('_shared', p, token, skipped)

    if skipped:
        print('\nNot uploaded (images and the like will come in a later stage):')
        for s in skipped:
            print('   -', s)
    print('\nDone.', call('GET', '/agents', token))


if __name__ == '__main__':
    main()
