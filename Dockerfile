FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency file
COPY pyproject.toml ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# Copy application code
COPY mailmate_search/ ./mailmate_search/

# Set Python path
ENV PYTHONPATH=/app
ENV TRANSFORMERS_CACHE=/app/data/models
ENV HF_HOME=/app/data/models

# Create data directories
RUN mkdir -p /app/data/chromadb /app/data/models

# Entrypoint
ENTRYPOINT ["python", "-m", "mailmate_search.cli"]

