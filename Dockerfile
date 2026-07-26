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
VOLUME ["/data"]
ENV DB_PATH=/data/ctb.db

RUN useradd --create-home --uid 10001 ctb && chown -R ctb:ctb /app
USER ctb

CMD ["python", "-m", "ctb"]
