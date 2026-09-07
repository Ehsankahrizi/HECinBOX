# syntax=docker/dockerfile:1.6
# ─────────────────────────────────────────────────────────────────────
# HECinBOX: Linux container — HEC-RAS compute engine + Streamlit UI.
#
# Two run modes:
#   1. Web UI (default):  docker run -p 8501:8501 -v ... hecinbox
#      Opens a browser form at http://localhost:8501
#   2. Headless CLI:      docker run -v ... hecinbox python -m main
#      Runs the pipeline from settings.yml directly.
# ─────────────────────────────────────────────────────────────────────

FROM --platform=linux/amd64 python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    SETTINGS_PATH=/app/settings.yml \
    PYTHONPATH=/app/src \
    HECRAS_ENGINE_DIR=/app/hecras_engine \
    MODELS_ROOT=/app/data/models \
    OUTPUT_DIR=/app/data/outputs

# Runtime system libraries for the HEC-RAS Fortran engine.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgfortran5 \
        libgomp1 \
        libxml2 \
        dos2unix \
        tzdata \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. Python deps (all manylinux wheels — no compiler needed).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 2. Application code + assets (logo, favicon).
COPY src/ /app/src/
COPY assets/ /app/assets/

# 3. HEC-RAS Linux compute engine + shipped Intel/MKL/glibc libs.
COPY hecras_engine/ /app/hecras_engine/

# 4. Default settings (overridden at runtime via UI or bind mount).
COPY config/settings.yaml /app/settings.yml

# 4b. Streamlit config (hides the Deploy button, disables telemetry).
COPY .streamlit/ /app/.streamlit/

# 5. Ensure engine launchers are LF-terminated and executable.
RUN dos2unix /app/hecras_engine/*.sh 2>/dev/null || true \
    && chmod -R a+rx /app/hecras_engine \
    && mkdir -p /app/data/models /app/data/outputs

# 6. Multi-process entrypoint — runs the auto-scheduler daemon and the
#    Streamlit web UI side-by-side so real-time forecasting survives
#    closed tabs, websocket drops, and Streamlit restarts.
COPY entrypoint.sh /app/entrypoint.sh
RUN dos2unix /app/entrypoint.sh 2>/dev/null || true \
    && chmod +x /app/entrypoint.sh

# 7. Engine binaries on PATH + library search path.
ENV PATH="/app/hecras_engine:${PATH}" \
    LD_LIBRARY_PATH="/app/hecras_engine/libs:/app/hecras_engine/libs/mkl:/app/hecras_engine/libs/rhel_8"

EXPOSE 8501

# Default: run the entrypoint (daemon + Streamlit web UI).
CMD ["/app/entrypoint.sh"]
