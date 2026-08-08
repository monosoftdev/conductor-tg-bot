# Pinned to the version CI tests on. `requires-python` allows newer, and the
# image ran 3.14 for a while — which meant the artifact that reaches production
# was the one interpreter the 2,200-test suite never executed against.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# README.md and LICENSE are build inputs, not documentation: `pyproject.toml`
# names them in `readme` and `license-files`, and hatchling reads both while
# generating metadata. Without them `pip install .` fails with
# "Readme file does not exist" — which passes every local check, because a
# checkout has them, and only shows up in the image build.
COPY pyproject.toml README.md LICENSE ./
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
# start if it is missing. Later migrations can ride the deploy instead — the
# pre-deploy step in `railway.toml` runs `python -m ctb.db.upgrade` from this
# image, in its own process, before this CMD ever starts.
CMD ["python", "-m", "ctb"]
