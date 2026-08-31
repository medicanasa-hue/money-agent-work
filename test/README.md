# MoneyPrinterTurbo Test Directory

This directory contains unit tests for the **MoneyPrinterTurbo** project.

## Directory Structure

- `services/`: Domain-focused unit and controller tests
  - `test_task.py`: Task pipeline tests
  - `test_task_manager.py`: In-memory and Redis queue tests
  - `test_controller_*.py`: API controller tests split by controller domain
  - `test_video.py`, `test_voice.py`: Media service tests
- `test_main.py`: Application entry-point test

## Running Tests

The CI suite uses pytest, which also runs the existing `unittest.TestCase`
tests:

```bash
# Run all tests
uv run python -X utf8 -m pytest -q test

# Run a specific test file
uv run python -X utf8 -m pytest -q test/services/test_video.py

# Run a specific test class
uv run python -X utf8 -m pytest -q test/services/test_video.py::TestVideoService

# Run a specific test method
uv run python -X utf8 -m pytest -q test/services/test_video.py::TestVideoService::test_preprocess_video
```

To run the same branch coverage check used by CI:

```bash
uv run python -X utf8 -m coverage run -m pytest -q test
uv run python -m coverage report
```

Live provider tests are skipped by default. To run tests that may call external
TTS or LLM services, set `MPT_RUN_INTEGRATION_TESTS=1` and provide the required
provider credentials.

Both Linux and Windows CI collect pytest functions and unittest cases. CI sets
`MPT_RUN_INTEGRATION_TESTS=0`, `HF_HUB_OFFLINE=1`, and
`TRANSFORMERS_OFFLINE=1` so provider calls and model downloads are not prerequisites.
The synthetic render smoke test uses local FFmpeg, bundled fonts, generated color
frames and a sine wave; it does not require API keys or user media:

```bash
uv run --no-sync python -m pytest -q test/services/test_render_smoke.py
```

Custom narration regressions cover WAV/MP3 uploads, optional loudness
normalization, interrupted-task recovery, and approximate-subtitle warnings:

```bash
uv run --no-sync python -X utf8 -m pytest -q test/services/test_custom_audio_validation.py test/services/test_custom_audio_resume.py test/services/test_custom_audio_smoke.py test/services/test_custom_audio_webui.py
```

The audio smoke tests create their own short signals and render them with real
FFmpeg. They check the final audio segment and caption visibility without calling
TTS or downloading a speech-recognition model. Run the suite in an isolated
checkout without production config or storage: some application modules load
local state at import time, before test fixtures take effect. The offline flags
above do not isolate those filesystem reads.

Symbolic-link boundary tests may skip on Windows without symlink privileges.
These skips should still be covered by Linux CI.

## Docker Runtime Acceptance

The CPU image installs runtime packages from `uv.lock` with `uv sync --locked
--no-dev`; pytest and other development tools are not required in the image.
Build from an isolated checkout and run the same media acceptance check with
standard-library unittest:

```bash
docker build --platform linux/amd64 -t mpt-local:verify .
docker run --rm --network none mpt-local:verify python -m unittest test.services.test_container_runtime
```

This checks direct runtime requirements, uv overrides (including Pillow),
installed lock versions, native imports, Turkish caption rendering, decoded
audio/video and upload rejection. It reuses the existing synthetic render test.
No host config, storage or model directories are mounted. A normal source bind
mount is deliberately absent from Compose, so source edits require a rebuild.

The optional publication workflow runs this check before publishing the exact
tested local image. `workflow_dispatch` defaults to build-only; publishing also
requires an explicit selection and the repository default branch. A successful
local run does not mean that GitHub CI or a GPU execution test has run.

## Adding New Tests

To add tests for other components, follow these guidelines:

1. Name files `test_<domain>.py` and keep each file focused on one domain.
2. Split broad controller suites into files such as `test_controller_video.py`.
3. Use either pytest functions or `unittest.TestCase`; pytest collects both.
4. Name test functions and methods with the `test_` prefix.

## Test Resources

Place any resource files required for testing in the `test/resources` directory.
