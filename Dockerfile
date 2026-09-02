# The lock file controls Python packages; pin the installer and base image too.
FROM ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 AS uv
FROM python:3.14-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f

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
