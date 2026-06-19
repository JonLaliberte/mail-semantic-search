FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Version stamping. The release workflow passes the resolved version + commit
# as build-args. .git is not in the build context (see .dockerignore), so
# SETUPTOOLS_SCM_PRETEND_VERSION feeds the version to setuptools-scm at install
# time. Defaults keep a plain `docker build` working (and versioned).
ARG APP_VERSION=0.0.0.dev0
ARG GIT_SHA=unknown
ENV APP_VERSION=${APP_VERSION} \
    GIT_SHA=${GIT_SHA} \
    SETUPTOOLS_SCM_PRETEND_VERSION=${APP_VERSION}

# Copy dependency file
COPY pyproject.toml ./

# Copy application code
COPY mail_semantic_search/ ./mail_semantic_search/

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# Set Python path
ENV PYTHONPATH=/app
ENV TRANSFORMERS_CACHE=/app/data/models
ENV HF_HOME=/app/data/models

# Create data directories
RUN mkdir -p /app/data/chromadb /app/data/models

# Entrypoint
ENTRYPOINT ["python", "-m", "mail_semantic_search.cli"]




