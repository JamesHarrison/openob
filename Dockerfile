# Debian Bookworm directly (not python:3.12-slim) so apt's python3-gi /
# python3-gst-1.0, which target Debian's system Python 3.11, are usable from
# the venv. The python:3.X-slim images ship their own Python under
# /usr/local, leaving apt's `gi` installed for a different interpreter and
# unreachable via --system-site-packages.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# GStreamer 1.22 (Bookworm) + GI bindings + redis-server. python3-gi has no
# wheel, so we install it via apt and build the venv with
# --system-site-packages so it's importable inside the venv.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        python3-pip \
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

# Install the package + test deps. Copy metadata first so this layer caches
# across source-only changes.
COPY pyproject.toml README.md ./
COPY openob ./openob
COPY tests ./tests
RUN pip install -e ".[test]"

CMD ["pytest", "tests/", "-v", "--timeout=60"]
