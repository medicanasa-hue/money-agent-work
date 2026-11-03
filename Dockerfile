# The lock file controls Python packages; pin the installer and base image too.
FROM ghcr.io/astral-sh/uv:0.12.9@sha256:8b940d3a9d65bed080436972241af2e21c84b5e8c9193f7014ed71479ee795ff AS uv
FROM python:3.15-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd

WORKDIR /MoneyPrinterTurbo
COPY --from=uv /uv /uvx /bin/

# Keep the Linux environment outside the source tree and use one FFmpeg binary.
ENV PYTHONPATH="/MoneyPrinterTurbo" \
    UV_PROJECT_ENVIRONMENT="/opt/venv" \
    UV_LINK_MODE="copy" \
    UV_PYTHON_DOWNLOADS="never" \
    PATH="/opt/venv/bin:$PATH" \
    IMAGEIO_FFMPEG_EXE="/usr/bin/ffmpeg" \
    FFMPEG_BINARY="/usr/bin/ffmpeg"

# A failed download or install must fail the build, never leave a partial image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock .python-version ./
# package=false: the application runs from source, so no second project install
# is needed. --locked also rejects a stale lock rather than silently rewriting it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python

COPY . .
EXPOSE 8501 8080

# Listen inside the container; Compose exposes both services on host loopback only.
CMD ["streamlit", "run", "./webui/Main.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.serverAddress=127.0.0.1", "--server.enableCORS=True", "--browser.gatherUsageStats=False", "--client.toolbarMode=minimal", "--logger.hideWelcomeMessage=True", "--server.showEmailPrompt=False"]
