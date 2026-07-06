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
    PYTHONWARNINGS="ignore:Unverified HTTPS request" \
    WEB_HOST=0.0.0.0

RUN pip install --no-cache-dir uv==0.9.5

WORKDIR ${WORKDIR_PATH}

# Layer 1: deps only — cached unless pyproject.toml / uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: project source + runtime assets.
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
COPY README.md ./

RUN uv sync --frozen --no-dev

CMD ["uv", "run", "--no-dev", "virtual-dev", "run", "--host", "0.0.0.0"]
