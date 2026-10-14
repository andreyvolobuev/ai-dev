# syntax=docker-hub.2gis.ru/docker-io/docker/dockerfile:1

# Default values are for local builds.
# In CI/CD PYTHON_REPOSITORY and PYTHON_TAG come from the devops pipeline.
ARG PYTHON_VERSION=3.13
ARG UBUNTU_RELEASE=24.04
ARG PYTHON_REPOSITORY=devops/library/python
ARG PYTHON_TAG=0.1.20

FROM docker-hub.2gis.ru/${PYTHON_REPOSITORY}-${PYTHON_VERSION}-ubuntu-${UBUNTU_RELEASE}:${PYTHON_TAG}

ENV WORKDIR_PATH=/app
ENV PYTHONUNBUFFERED=1 \
    PYTHONWARNINGS="ignore:Unverified HTTPS request"

# The container runs as root, and the spawned `claude` CLI is invoked with
# --dangerously-skip-permissions (permission_mode="bypassPermissions"). Claude
# Code blocks that flag under uid 0 unless it's told the environment is already
# isolated. The pod IS that sandbox, so assert it explicitly.
ENV IS_SANDBOX=1

# git is a hard runtime dependency: the bot clones/fetches/pushes target
# repos into workspaces/ (GitLabVcs shells out to the git CLI). The base
# python image doesn't ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.9.5

WORKDIR ${WORKDIR_PATH}

# Layer 1: deps only — cached unless pyproject.toml / uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 1.5: bake the embedding model into the image.
#
# fastembed downloads it lazily, on the first embed() call — which in
# practice is the first search_mr_history, i.e. in the MIDDLE of an agent
# run. The container's cache is ephemeral, so that download happened on
# EVERY start: ~0.5GB pulled from huggingface.co over the network, plus
# the download buffers on top of an already-running agent, plus an
# unauthenticated HF dependency ("set a HF_TOKEN to enable higher rate
# limits") that breaks the tool whenever HF throttles or is unreachable.
# Baked here, the model is just there: no network, no warning, no spike.
#
# Must match FastembedEmbedder._DEFAULT_MODEL. Setting EMBEDDER_MODEL at
# runtime to anything else brings the runtime download back.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
ARG EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
RUN .venv/bin/python -c "\
from fastembed import TextEmbedding; \
TextEmbedding(model_name='${EMBEDDING_MODEL}')"

# Layer 2: project source + runtime assets.
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
COPY README.md ./

RUN uv sync --frozen --no-dev

CMD ["uv", "run", "--no-dev", "virtual-dev", "run", "--host", "0.0.0.0"]
