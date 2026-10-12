# Profi.ru monitor

Page: https://devorvant.ru/profi-monitor.html

Launch `profi_worker_entry.py` on Windows. It downloads the capture worker and
its transport module from the same current GitHub commit. Existing credentials
and folders stay beside the launcher; capture, rename and window-close behavior
are retained.

The PC saves the complete MHTML locally and uploads only card HTML and capture
metadata to `profi_inputs/YYYY-MM-DD/<capture_id>.json`. It does not parse order
fields. Backoffice navigation, scripts, images and unrelated account controls
are excluded from the transmitted document.

GitHub Actions runs `scripts/profi_ingest.py` and `scripts/profi_parser.py`,
creates `profi_latest.json` and parsed archives, then updates history and metrics.
Changing the server parser reparses stored HTML automatically; it requires no
replacement of files on the PC. The launcher also gets Windows-worker updates
automatically at the next run.

CLI: `python profi_worker_entry.py` (or `-a`). To send an already saved file:
`python profi_worker_entry.py --upload-only saved_pages/your_capture.mhtml`.

The workflow builds:

- `history/profi_parsed/YYYY-MM-DD/<capture_id>.json`: server-parsed reports.
- `profi_input_state.json`: source checksums and parser fingerprint.
- `profi_current.json`: orders from the newest saved capture and profile tags.
- `profi_history.json`: first/last observation, scan count, observed dates by ID.
- `profi_pipeline_status.json`: latest metrics, daily unique counts and run list.
- `history/profi/YYYY-MM-DD/<scan_id>.json`: individual capture snapshots.

Older uploads are recovered from the Git history of `profi_latest.json` so CI
does not lose a capture uploaded between runs. A reparse of the collector's
same timestamped file replaces its snapshot and does not add another scan.
Empty/invalid latest inputs fail before replacing existing results.

Dates and daily boundaries use Moscow time. First observed means the first
appearance in our captures, not the customer's publication time. Absence in a
later capture does not establish that an order was closed. Daily counts are
unique IDs across that day's saved captures; days without captures are gaps.

Profile marks are transparent keyword rules for controls, modeling, embedded
systems, engineering and adjacent data tasks. They are not a probability or
an AI assessment. Budgets retain upper/lower bounds and units; hourly prices
are not averaged with project prices.

Older collector reports remain readable and carry a visible parser warning.
Use the new launcher for corrected server-side carousel/price extraction.

Local checks:

```sh
python -m unittest discover -s tests -p 'test_profi_monitor.py'
python scripts/profi_ingest.py
python scripts/profi_monitor.py --git-backfill
```

Public capture uploads are disabled pending explicit authorization for order-card
contents in the public repository. Server tests use synthetic inputs only.
