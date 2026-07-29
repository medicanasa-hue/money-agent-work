# Use OpenMontage with MPT

OpenMontage is a local library of finished explanatory animations under
`OpenMontage/projects/`. MPT never starts Manim while a video task is running;
it only discovers and uses an already rendered local MP4.

## Use native output for the requested frame

Do not turn a landscape OpenMontage render into a vertical Short by cropping
it. For a `9:16` MPT task, use a native portrait output such as:

```text
OpenMontage/projects/money-printing-inflation/final_silent_tr_9x16_1080p.mp4
```

Native output names use these aspect labels:

| MPT aspect | OpenMontage label |
| --- | --- |
| `9:16` | `9x16` |
| `4:5` | `4x5` |
| `1:1` | `1x1` |
| `16:9` | `16x9` |

Turkish renders include `_tr` after `final_silent`; English is the default
name. If a native output for the selected aspect and language does not exist,
MPT leaves OpenMontage unselected instead of cropping a different aspect.

## Verify the local library

Run this read-only check before relying on a newly rendered project:

```powershell
cd C:\A\money
uv run python cli.py --validate-openmontage
```

It checks project manifests, output dimensions, duration, and basic encoding
metadata. It does not invoke Manim, upload anything, or publish anything.

## Use one output explicitly from the CLI

```powershell
cd C:\A\money
uv run python cli.py `
  --video-subject "Para basımı enflasyonu nasıl etkiler?" `
  --video-source local `
  --video-materials "OpenMontage\projects\money-printing-inflation\final_silent_tr_9x16_1080p.mp4" `
  --video-aspect 9:16
```

For a normal WebUI task, choosing `local` shows a matching OpenMontage output
as a suggestion. It is never selected automatically; confirm it yourself.

Scheduled jobs can opt in separately with `openmontage_auto_materials = true`.
They only choose a native, language-matched local output that already exists;
they never render OpenMontage or publish a video.

## Create or refresh an OpenMontage output

Render the project itself first, outside MPT. For example, the native portrait
renderer in the money-printing project is invoked from that project directory:

```powershell
cd C:\A\money\OpenMontage\projects\money-printing-inflation
python render_portrait.py --language tr --aspect 9:16 --quality production
```

`production` uses the project's lower-loss local encoding path. Render each
target aspect directly rather than upscaling or cropping an existing file.
