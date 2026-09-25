# PipelineGPT backend image. One image serves every role (see docker-entrypoint.sh):
#   api | worker | flower | migrate | create-admin <email>
#
# Models (embedder, reranker, spaCy for PII) are baked in so the container runs
# with no internet access at runtime.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv

# CPU-only torch first, so sentence-transformers doesn't pull the multi-GB CUDA build.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install .

RUN python -m spacy download en_core_web_sm

ENV FASTEMBED_CACHE_PATH=/opt/models/fastembed \
    HF_HOME=/opt/models/huggingface
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')" \
    && python -c "from sentence_transformers import CrossEncoder; CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"


FROM python:${PYTHON_VERSION}-slim AS runtime

ARG VERSION=dev
LABEL org.opencontainers.image.title="PipelineGPT backend" \
      org.opencontainers.image.description="RAG API for pipeline integrity data with engineer review (HITL)" \
      org.opencontainers.image.source="https://github.com/Kubu-Ventures/pipe-line_gpt-backend" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.version="${VERSION}"

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE_PATH=/opt/models/fastembed \
    HF_HOME=/opt/models/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    APP_VERSION=${VERSION}

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models

WORKDIR /app
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
COPY --chown=app:app create_admin.py seed_demo.py docker-entrypoint.sh ./

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4)"]

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["api"]
