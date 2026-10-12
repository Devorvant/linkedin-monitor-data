"""Index ALREADY-PUBLISHED Git versions of Profi recommendations.

This module writes only dates, counts and Git references. It never writes copies
of order titles, descriptions, conditions, reasons or drafts to history files.
The site reads the original public file at its existing Git commit.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from profi_monitor import date_time, read_json, write_json

ACTIONS = {'respond', 'clarify', 'skip'}


def version_metadata(raw, ref):
    if not re.fullmatch(r'(?:[a-f0-9]{40}|main)', ref):
        raise ValueError('Invalid Git reference')
    if not isinstance(raw, dict) or not isinstance(raw.get('items'), list) or not raw['items']:
        raise ValueError('No recommendation items; previous history retained')
    captured = date_time(raw['captured_at'])
    analyzed = date_time(raw['generated_at'])
    ids, stable = set(), []
    for item in raw['items']:
        if not isinstance(item, dict):
            raise ValueError('Invalid recommendation item')
        ident = str(item.get('order_id', ''))
        if not re.fullmatch(r'\d+', ident) or ident in ids or item.get('recommendation') not in ACTIONS:
            raise ValueError('Invalid/duplicate recommendation ID or action')
        ids.add(ident)
        row = {k: v for k, v in item.items() if k not in {'analyzed_at', 'first_seen', 'last_seen'}}
        if row.get('analysis_source') in {'ai', 'ai_cache'}:
            row['analysis_source'] = 'ai'
        stable.append(row)
    identity = {'capture': captured.isoformat(), 'model': raw.get('model'),
        'version': raw.get('analysis_version'), 'items': sorted(stable, key=lambda r: r['order_id'])}
    ident = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    counts = Counter(item['recommendation'] for item in raw['items'])
    # Metadata only: do not persist the private content processed above.
    meta = {'snapshot_id': ident, 'date': captured.date().isoformat(),
        'captured_at': captured.isoformat(timespec='seconds'),
        'analyzed_at': analyzed.isoformat(timespec='seconds'), 'ref': ref,
        'path': 'profi_recommendations.json', 'status': raw.get('status'),
        'orders': len(ids), 'ai_orders': sum(r.get('analysis_source') in {'ai', 'ai_cache'} for r in raw['items']),
        'respond': counts['respond'], 'clarify': counts['clarify'], 'skip': counts['skip']}
    return meta, ids


def build_index(versions):
    runs, ids, daily = {}, set(), {}
    for raw, ref in versions:
        meta, observed = version_metadata(raw, ref)
        old = runs.get(meta['snapshot_id'])
        if old is None or meta['analyzed_at'] < old['analyzed_at'] or old['ref'] == 'main' and ref != 'main':
            runs[meta['snapshot_id']] = meta
        ids.update(observed)
        daily.setdefault(meta['date'], set()).update(observed)
    if not runs:
        raise ValueError('No valid recommendation versions; previous index retained')
    ordered = sorted(runs.values(), key=lambda r: (r['captured_at'], r['analyzed_at'], r['snapshot_id']), reverse=True)
    return {'schema_version': 2, 'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'runs': ordered, 'summary': {'snapshots': len(ordered), 'unique_orders': len(ids), 'days': len(daily),
            'first_capture': ordered[-1]['captured_at'], 'last_capture': ordered[0]['captured_at']},
        'daily': [{'date': d, 'unique_orders': len(v)} for d, v in sorted(daily.items())],
        'scope_note': 'Индекс уже опубликованных версий файла в Git. Архив не подтверждает, что заказ открыт.'}


def published_versions(root):
    result = subprocess.run(['git', 'log', '--reverse', '--format=%H', '--', 'profi_recommendations.json'],
                            cwd=root, capture_output=True, text=True, check=True)
    versions = []
    for commit in result.stdout.splitlines():
        old = subprocess.run(['git', 'show', commit + ':profi_recommendations.json'],
                             cwd=root, capture_output=True, text=True)
        if old.returncode:
            continue
        try:
            raw = json.loads(old.stdout)
            version_metadata(raw, commit)
            versions.append((raw, commit))
        except (ValueError, KeyError, TypeError):
            print('Invalid published recommendation version skipped: ' + commit[:8])
    return versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--git-backfill', action='store_true')
    args = parser.parse_args()
    root = Path(args.root)
    versions = published_versions(root) if args.git_backfill else []
    # This same existing file is committed by the existing monitor pipeline.
    # "main" resolves its latest version; the next build pins it to its commit.
    versions.append((read_json(root / 'profi_recommendations.json'), 'main'))
    result = build_index(versions)
    write_json(root / 'profi_recommendation_history.json', result)
    print(json.dumps(result['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
