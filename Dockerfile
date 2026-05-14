FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

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




