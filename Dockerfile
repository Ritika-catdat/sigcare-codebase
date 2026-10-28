# Sigcare Document Quality API
# Build:  docker compose build
# Run:    docker compose up -d
# Health: curl http://localhost:8000/health

FROM python:3.11-slim AS runtime

# OMP_THREAD_LIMIT=1: Tesseract's own OpenMP threads spin-wait and fight each
# other in containers; the pipeline runs several single-threaded Tesseract
# processes in parallel instead (SIGCARE_OCR_WORKERS).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_THREAD_LIMIT=1

# System packages:
#  - tesseract-ocr: needed by pytesseract when page counters are not in the PDF text layer
#  - libglib2.0-0:  runtime dependency of opencv-python-headless
#  - curl:          used by the container HEALTHCHECK
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so this layer is cached between code changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Non-root user; api_outputs is the job output volume mounted by docker-compose.
RUN useradd --create-home --uid 1000 sigcare \
    && mkdir -p /app/api_outputs \
    && chown -R sigcare:sigcare /app
USER sigcare

ENV SIGCARE_API_OUTPUT_DIR=/app/api_outputs \
    SIGCARE_HOST=0.0.0.0 \
    SIGCARE_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${SIGCARE_PORT}/health" || exit 1

CMD ["sh", "-c", "uvicorn sigcare_quality.api:app --host ${SIGCARE_HOST} --port ${SIGCARE_PORT} --workers ${SIGCARE_WORKERS:-1}"]
