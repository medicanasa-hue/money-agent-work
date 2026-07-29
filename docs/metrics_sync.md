# Publish metrics synchronization

MoneyPrinterTurbo can refresh analytics for previously published jobs without
creating a new video. It only considers jobs that have upload records and whose
metrics are missing or older than the normal recheck window.

Start with a no-network preview:

```powershell
cd C:\A\money
uv run python cli.py --sync-metrics --sync-metrics-dry-run --sync-metrics-limit 20
```

Run the actual synchronization after confirming the count:

```powershell
uv run python cli.py --sync-metrics --sync-metrics-limit 20
```

`--sync-metrics-limit` keeps each run bounded. Omit it only when a full backlog
run is intentional. If Upload-Post is not configured, the command exits with a
clear message and does not attempt a network request. API keys are never printed.
An actual synchronization run returns a nonzero exit code when one or more jobs
encounter an error, so Task Scheduler can report the failure.

## Windows Task Scheduler

Create a task that runs under the same Windows account that has `uv` and the
MoneyPrinterTurbo configuration available. A conservative hourly action is:

```powershell
powershell.exe -NoProfile -Command "Set-Location 'C:\A\money'; uv run python cli.py --sync-metrics --sync-metrics-limit 20"
```

Use the dry-run command first when changing the schedule or limit. The command
applies request pacing and retries transient analytics failures, so a small limit
is preferable to a long, unrestricted task on the first run.
