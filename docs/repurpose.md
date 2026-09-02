# Short Clip Repurposing

Create a non-destructive short-clip plan from a local video:

```powershell
uv run python cli.py --repurpose-video "C:\videos\long.mp4" --repurpose-clip-duration 30 --repurpose-clip-count 3
```

This prints JSON clip windows only. It does not create files, start a video
generation task, upload anything, or publish anything.

To favor high-signal moments from an existing local subtitle file, add the
optional `--repurpose-subtitle-file` flag:

```powershell
uv run python cli.py --repurpose-video "C:\videos\long.mp4" --repurpose-clip-duration 30 --repurpose-clip-count 3 --repurpose-subtitle-file "C:\videos\long.srt"
```

The selector stays local and uses subtitle timing plus simple hook signals. If
the SRT file is missing or does not contain enough separate candidates, it
keeps the balanced clip plan instead. The subtitle path is never included in
the JSON result.

To write the planned clips to an explicit local directory, add
`--repurpose-output-dir`:

```powershell
uv run python cli.py --repurpose-video "C:\videos\long.mp4" --repurpose-clip-duration 30 --repurpose-clip-count 3 --repurpose-output-dir "C:\videos\short-clips"
```

Rendered files are named `short_clip_01.mp4`, `short_clip_02.mp4`, and so on.
Existing files are never overwritten. The first rendering mode uses fast stream
copy, so cut points can align to nearby video keyframes. It preserves the video
and available audio stream without calling external services.

For cuts that more closely honor the planned timestamps, opt into precise
re-encoding:

```powershell
uv run python cli.py --repurpose-video "C:\videos\long.mp4" --repurpose-clip-duration 30 --repurpose-clip-count 3 --repurpose-output-dir "C:\videos\short-clips" --repurpose-render-mode precise
```

`precise` is slower because it re-encodes video with `libx264` and audio with
AAC. Omitting `--repurpose-render-mode` keeps the faster stream-copy default.

To make a clean vertical short from a landscape source, opt into the portrait
output frame together with precise rendering:

```powershell
uv run python cli.py --repurpose-video "C:\videos\long.mp4" --repurpose-clip-duration 30 --repurpose-clip-count 3 --repurpose-output-dir "C:\videos\short-clips" --repurpose-render-mode precise --repurpose-aspect 9:16
```

This fills a 1080x1920 frame with a centered crop and does not add or alter
subtitles. It is intentionally unavailable in fast stream-copy mode, where a
crop would require re-encoding.

To use a supported hardware encoder for precise rendering, pass the existing
video codec option. For example, this machine's tested AMF encoder can be used
with:

```powershell
uv run python cli.py --repurpose-video "C:\videos\long.mp4" --repurpose-clip-duration 30 --repurpose-clip-count 3 --repurpose-output-dir "C:\videos\short-clips" --repurpose-render-mode precise --video-codec h264_amf
```

When omitted, precise rendering uses `libx264`. The codec option has no effect
on fast stream-copy rendering.
