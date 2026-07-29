# Scheduled video jobs

Named jobs let Windows Task Scheduler run a repeatable video generation
command without placing a subject or script in the scheduler action itself.

Add a job to the `[app]` section of `config.toml`:

```toml
[[app.scheduled_jobs]]
name = "weekday-finance"
enabled = true
video_subject = "Why grocery prices can rise before wages do"
video_script = ""
```

Leave `video_script` empty to generate it from the subject, or provide an
approved script. Job names are case-insensitive and must be unique.

List the configured job names and their enabled state without exposing scripts:

```powershell
uv run python cli.py --list-scheduled-jobs
```

Validate the configuration without generating a video:

```powershell
cd C:\A\money
uv run python cli.py --scheduled-job weekday-finance --scheduled-job-dry-run
```

Run the job after the dry run succeeds:

```powershell
uv run python cli.py --scheduled-job weekday-finance
```

Scheduled runs always require a usable viral quality score and apply
`viral_quality_gate_threshold`, even when the WebUI's warning-only gate is
disabled. A low or unavailable score returns a nonzero exit code without
starting video generation.

If Upload-Post is configured, generated uploads remain in the review queue;
the scheduler never publishes them automatically. The generated job is also
saved to history so it can be reviewed from the WebUI.

## Windows Task Scheduler

Create the task under the same Windows account that can run `uv` and access
the project configuration. Use this action:

```powershell
powershell.exe -NoProfile -Command "Set-Location 'C:\A\money'; uv run python cli.py --scheduled-job weekday-finance"
```

Use the dry-run command after changing the job name or configuration. The CLI
prints only a small status summary and never prints API keys.
