# Multi-stage build: wheels are compiled in a throwaway stage so the runtime image
# carries no build toolchain and therefore a smaller attack surface.
FROM python:3.11-slim AS builder

WORKDIR /build
# Install from requirements.txt: every direct dependency is pinned to an exact version. The
# hash-pinned requirements.lock is committed for local reproducibility, but it is generated
# per-platform, so --require-hashes cannot verify Linux wheels from a Windows-authored lock -
# using it here would break this linux/amd64 build.
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.11-slim

# Run as an unprivileged user. A container that never needs root should never have it.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels /wheels/* \
    && rm -rf /wheels

COPY --chown=appuser:appuser app ./app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_FORMAT=json

EXPOSE 8000

# Uses the liveness probe, which deliberately does not depend on the portal - a portal
# outage must not make the orchestrator kill a healthy container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
