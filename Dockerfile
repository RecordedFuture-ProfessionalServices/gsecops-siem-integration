# Container image for the Recorded Future risklist ingestion script.
#
# Only the Kubernetes deployment in deploy/kubernetes needs this image. The
# Cloud Run Function deployment described in README.md does not use it.
#
# Build from the repository root:
#   docker build -t rf-risklist-ingest:1.0 .

FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# requirements.txt installs psengine from ./deps, so the wheel must be in place
# before pip runs.
COPY src/requirements.txt ./
COPY src/deps ./deps
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim

# Unbuffered output keeps the script's progress visible in `kubectl logs` while
# a run is still in flight. HOME points at /tmp because the Kubernetes deployment
# runs with a read-only root filesystem and mounts an emptyDir at /tmp.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    PATH="/opt/venv/bin:${PATH}"

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY src/ ./
# The wheel is already installed into the venv; it only bloats the final image.
RUN rm -rf ./deps

USER 1000:1000

ENTRYPOINT ["python", "main.py"]
