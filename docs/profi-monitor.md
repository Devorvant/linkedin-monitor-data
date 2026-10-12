# Profi.ru monitor

Page: https://devorvant.ru/profi-monitor.html

The Windows collector v4_4 uploads `profi_latest.json` as before. A push updates
the dedicated `Build Profi Monitor` workflow; no new desktop credentials or
mouse coordinates are required.

The workflow builds:

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
Run v4_4 for the corrected carousel/price extraction.

Local checks:

```sh
python -m unittest discover -s tests -p 'test_profi_monitor.py'
python scripts/profi_monitor.py --git-backfill
```
