# Profi.ru monitor

Page: https://devorvant.ru/profi-monitor.html

The architecture matches LinkedIn:

1. `profi_worker_entry.py` downloads the current `scripts/profi_worker_unified.py`
   from one pinned GitHub commit into the PC's `cache` folder.
2. The worker runs on the Windows PC: scroll, save MHTML, rename, close Chrome,
   parse order cards locally and upload `profi_latest.json`.
3. GitHub Actions updates snapshots, first/last observations and metrics.
4. The website reads `profi_current.json`, `profi_history.json` and
   `profi_pipeline_status.json`.

Place the launcher beside the existing `saved_pages`, `reports` and
`github_token.txt`. Run `python profi_worker_entry.py` or add `-a`.
Existing coordinate options are forwarded, e.g. `--close-y 0.02`.
`--parse-only saved_pages/your_capture.mhtml` sends a parsed JSON report from an
existing local MHTML without opening Chrome.

The complete MHTML and card HTML stay on the PC. Only the existing structured
JSON report is uploaded, as with the earlier v4 collector. Old launchers which
request `profi_capture_worker.py` are kept compatible with the same local parser.
Server-side raw-HTML ingest is no longer wired into the workflow.

History:

- `history/profi/YYYY-MM-DD/<scan_id>.json`: normalized saved JSON snapshots.
- `profi_history.json`: first/last observation, scan count, observed dates by ID.
- `profi_current.json`: newest capture and explained profile-keyword tags.
- `profi_pipeline_status.json`: metrics, daily unique counts and run list.

Git backfill recovers earlier `profi_latest.json` uploads between CI runs.
Reparsing the same timestamped file updates one snapshot, without adding another
scan. Moscow time is used for daily boundaries. First observed is not the
customer's publication time; absence in a later scan does not establish closure.

The profile tags use explained keyword rules and require manual review.
Price bounds and units are preserved; hourly prices are not averaged with project
prices. Invalid/empty parser results leave the previous JSON intact.

Checks:

```sh
python -m unittest discover -s tests -p 'test_profi*.py'
python scripts/profi_monitor.py --git-backfill
```
