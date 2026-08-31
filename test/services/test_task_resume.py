import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models import const
from app.models.schema import VideoParams
from app.services import task as tm


class TestInterruptedTaskResume(unittest.TestCase):
    def test_resume_reuses_saved_script_and_terms(self):
        task_id = "interrupted-task"
        params = VideoParams(video_subject="saved subject", video_source="pexels")

        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = Path(temp_dir) / "tasks"
            task_directory = task_root / task_id
            task_directory.mkdir(parents=True)
            (task_directory / "script.json").write_text(
                json.dumps(
                    {
                        "script": "Saved script.",
                        "search_terms": ["saved term"],
                        "params": params.model_dump(mode="json"),
                    }
                ),
                encoding="utf-8",
            )
            state = SimpleNamespace(
                get_task=MagicMock(
                    return_value={
                        "task_id": task_id,
                        "state": const.TASK_STATE_FAILED,
                        "interrupted": True,
                    }
                )
            )

            with (
                patch.object(tm.sm, "state", state),
                patch.object(
                    tm.utils,
                    "task_dir",
                    side_effect=lambda part="": str(task_root / part),
                ),
                patch.object(
                    tm, "start", return_value={"videos": ["final.mp4"]}
                ) as start,
            ):
                result = tm.resume_interrupted_task(task_id)

        self.assertEqual(result, {"videos": ["final.mp4"]})
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["task_id"], task_id)
        self.assertEqual(
            start.call_args.kwargs["params"].video_subject, "saved subject"
        )
        self.assertEqual(start.call_args.kwargs["resume_video_script"], "Saved script.")
        self.assertEqual(start.call_args.kwargs["resume_video_terms"], ["saved term"])
        self.assertTrue(start.call_args.kwargs["require_upload_review"])

    def test_resume_refuses_task_that_was_not_interrupted(self):
        state = SimpleNamespace(
            get_task=MagicMock(
                return_value={
                    "task_id": "failed-task",
                    "state": const.TASK_STATE_FAILED,
                }
            )
        )

        with (
            patch.object(tm.sm, "state", state),
            patch.object(tm, "start") as start,
        ):
            result = tm.resume_interrupted_task("failed-task")

        self.assertIsNone(result)
        start.assert_not_called()

    def test_start_uses_resume_values_without_calling_llm(self):
        params = VideoParams(video_subject="saved subject", video_source="pexels")
        state = SimpleNamespace(update_task=MagicMock())

        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm, "generate_script", side_effect=AssertionError("must not run")
            ),
            patch.object(
                tm, "generate_terms", side_effect=AssertionError("must not run")
            ),
            patch.object(tm, "save_script_data"),
        ):
            result = tm._start(
                "resume-task",
                params,
                stop_at="terms",
                resume_video_script="Saved script.",
                resume_video_terms=["saved term"],
            )

        self.assertEqual(
            result,
            {"script": "Saved script.", "terms": ["saved term"]},
        )

    def test_resume_reuses_saved_audio_and_subtitle_when_available(self):
        task_id = "interrupted-audio-task"
        params = VideoParams(video_subject="saved subject", video_source="pexels")

        with tempfile.TemporaryDirectory() as temp_dir:
            task_root = Path(temp_dir) / "tasks"
            task_directory = task_root / task_id
            task_directory.mkdir(parents=True)
            (task_directory / "script.json").write_text(
                json.dumps(
                    {
                        "script": "Saved script.",
                        "search_terms": ["saved term"],
                        "params": params.model_dump(mode="json"),
                    }
                ),
                encoding="utf-8",
            )
            audio_file = task_directory / "audio.normalized.wav"
            subtitle_file = task_directory / "subtitle.srt"
            audio_file.write_bytes(b"audio")
            subtitle_file.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nSaved.\n", encoding="utf-8"
            )
            state = SimpleNamespace(
                get_task=MagicMock(
                    return_value={
                        "task_id": task_id,
                        "state": const.TASK_STATE_FAILED,
                        "interrupted": True,
                    }
                )
            )

            with (
                patch.object(tm.sm, "state", state),
                patch.object(
                    tm.utils,
                    "task_dir",
                    side_effect=lambda part="": str(task_root / part),
                ),
                patch.object(tm.voice, "get_audio_duration", return_value=6.1),
                patch.object(
                    tm, "start", return_value={"videos": ["final.mp4"]}
                ) as start,
            ):
                tm.resume_interrupted_task(task_id)

            self.assertEqual(
                Path(start.call_args.kwargs["resume_audio_file"]).resolve(strict=True),
                audio_file.resolve(strict=True),
            )
            self.assertEqual(start.call_args.kwargs["resume_audio_duration"], 7)
            self.assertEqual(
                Path(start.call_args.kwargs["resume_subtitle_path"]).resolve(strict=True),
                subtitle_file.resolve(strict=True),
            )

    def test_start_reuses_saved_audio_and_subtitle_without_regeneration(self):
        params = VideoParams(video_subject="saved subject", video_source="pexels")
        state = SimpleNamespace(update_task=MagicMock())

        with (
            patch.object(tm.sm, "state", state),
            patch.object(
                tm, "preflight_custom_audio", return_value={"selected": False}
            ),
            patch.object(
                tm, "generate_script", side_effect=AssertionError("must not run")
            ),
            patch.object(
                tm, "generate_terms", side_effect=AssertionError("must not run")
            ),
            patch.object(
                tm, "generate_audio", side_effect=AssertionError("must not run")
            ),
            patch.object(
                tm, "generate_subtitle", side_effect=AssertionError("must not run")
            ),
            patch.object(tm, "save_script_data"),
        ):
            result = tm._start(
                "resume-task",
                params,
                stop_at="subtitle",
                resume_video_script="Saved script.",
                resume_video_terms=["saved term"],
                resume_audio_file="saved-audio.wav",
                resume_audio_duration=7,
                resume_subtitle_path="saved-subtitle.srt",
            )

        self.assertEqual(result, {"subtitle_path": "saved-subtitle.srt"})
