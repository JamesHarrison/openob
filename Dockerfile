FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# GStreamer 1.22 (Debian 12 / Bookworm) + the GI bindings + redis-server.
# python3-gi has no wheel, so we install it via apt and build a venv with
# --system-site-packages so it's visible to our pip-installed package.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-libav \
        gir1.2-gstreamer-1.0 \
        python3-gi \
        python3-gst-1.0 \
        redis-server \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages "$VIRTUAL_ENV" \
    && pip install --upgrade pip setuptools wheel

WORKDIR /app

# Install the package + test deps. Copy only the metadata first so this layer
# caches across source-only changes.
COPY pyproject.toml README.md ./
COPY openob ./openob
COPY tests ./tests
RUN pip install -e ".[test]"

CMD ["pytest", "tests/", "-v", "--timeout=60"]
