FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# No volume and no writable path: every byte of state lives in PostgreSQL. The
# image is stateless, so a redeploy carries nothing across but the database —
# which also retires the whole `RAILWAY_RUN_UID=0` volume-ownership trap that
# the SQLite build needed.
RUN useradd --create-home --uid 10001 ctb && chown -R ctb:ctb /app
USER ctb

# The application never applies DDL. Run `python -m ctb.db.bootstrap` once, as
# an operator, before the first deploy; boot verifies the schema and refuses to
# start if it is missing.
CMD ["python", "-m", "ctb"]
