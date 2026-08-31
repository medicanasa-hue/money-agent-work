"""Exercise the media toolchain using synthetic local inputs, without API calls."""

import io
import subprocess
from pathlib import Path
from unittest.mock import patch

from moviepy import VideoFileClip

from app.config import config
from app.models.schema import VideoParams
from app.services import material_upload, video
from app.utils import utils


def test_turkish_subtitle_render_and_material_validation(tmp_path):
    ffmpeg = utils.get_ffmpeg_binary()
    source = tmp_path / "source.mp4"
    audio = tmp_path / "narration.wav"
    subtitles = tmp_path / "captions.srt"
    output = tmp_path / "turkce-smoke.mp4"

    def run_ffmpeg(*arguments):
        subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-y", *arguments],
            check=True,
            timeout=45,
            capture_output=True,
        )

    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=0x13263b:s=1080x1080:r=24",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(source),
    )
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "1",
        "-c:a",
        "pcm_s16le",
        str(audio),
    )
    subtitles.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nTÜRKÇE DENEME\nİstanbul: ışık, gölge\nÜçüncü satır\n\n",
        encoding="utf-8",
    )
    params = VideoParams(
        video_subject="offline smoke",
        video_aspect="1:1",
        bgm_type="",
        bgm_volume=0,
        font_name="BeVietnamPro-Medium.ttf",
        font_size=48,
        stroke_width=6,
        text_background_color=False,
        n_threads=2,
    )
    with patch.dict(config.app, {"video_codec": "libx264", "video_fps": 24}):
        video.generate_video(
            str(source),
            str(audio),
            str(subtitles),
            str(output),
            params,
            prefer_ffmpeg_srt_subtitles=False,
        )
    with VideoFileClip(str(output)) as rendered:
        assert rendered.size == [1080, 1080]
        assert 0.95 <= rendered.duration <= 1.1
        assert rendered.audio is not None
        # The synthetic source is dark blue: bright pixels prove the caption was burned in.
        frame = rendered.get_frame(0.5)
        assert (frame.min(axis=2) > 220).sum() > 500
        assert rendered.get_frame(0.9).shape == (1080, 1080, 3)
    run_ffmpeg("-xerror", "-i", str(output), "-f", "null", "-")

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    with patch.object(
        material_upload, "uploaded_material_dir", return_value=str(uploads)
    ):
        with output.open("rb") as stream:
            stored = material_upload.save_material_upload("Türkçe video.MP4", stream)
        assert Path(uploads, stored).is_file()
        # Autodetection must not allow a renamed playlist to fetch remote content.
        playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\nhttp://127.0.0.1:9/do-not-fetch.ts\n#EXT-X-ENDLIST\n"
        try:
            material_upload.save_material_upload("playlist.mp4", io.BytesIO(playlist))
        except material_upload.MaterialUploadError:
            pass
        else:
            raise AssertionError("a renamed playlist was accepted")
