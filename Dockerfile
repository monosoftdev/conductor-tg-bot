FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# The SQLite state lives on a Railway volume mounted here. It holds the
# transcript cursors — losing it means re-seeking, not replaying.
ENV DB_PATH=/data/ctb.db

RUN useradd --create-home --uid 10001 ctb \
    && mkdir -p /data \
    && chown -R ctb:ctb /app /data
# Deliberately NO `VOLUME ["/data"]`. Railway attaches volumes at runtime from
# the dashboard, so the instruction buys nothing here, and Railway's builder
# rejects it — the image build fails in seconds with only "Failed to build an
# image". Plain directory + a runtime writability check (below) instead.
USER ctb

# Preflight, then hand the PID to Python. The `chown` above only covers the
# image's own /data: Railway mounts a *fresh, root-owned* volume over it, and
# this image runs as uid 10001 — SQLite would then fail with a bare
# "unable to open database file" and crash-loop ten times with no hint. Say the
# fix instead. (Docker's own named volumes inherit image ownership, so a local
# `docker run` cannot reproduce this; see README "Before your first deploy".)
CMD ["sh", "-c", "dir=$(dirname \"${DB_PATH:-/data/ctb.db}\"); [ -w \"$dir\" ] || { echo \"FATAL: cannot write $dir (DB_PATH=${DB_PATH:-/data/ctb.db}) as uid $(id -u). On Railway: set RAILWAY_RUN_UID=0 and mount the volume at /data.\" >&2; exit 1; }; exec python -m ctb"]
