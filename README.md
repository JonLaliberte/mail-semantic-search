# MailMate AI Search Tool

AI-powered semantic search for MailMate emails using local embeddings and vector search.

## Features

- **Local AI Embeddings**: Uses sentence-transformers models (default: BGE-base-en-v1.5) running entirely on your machine
- **Semantic Search**: Find emails by meaning, not just keywords
- **Dockerized**: Easy setup with Docker and Docker Compose
- **Incremental Indexing**: Only indexes new or changed emails
- **Privacy-First**: All processing happens locally, no external APIs

## Requirements

- Docker and Docker Compose
- Mac Mini M4 with 24GB RAM (or similar hardware)
- MailMate email client with emails stored as .eml files

## Quick Start

1. **Clone or navigate to the project directory**

2. **Create `.env` file:**
   ```bash
   # Create .env file with the following content:
   # Note: MAILMATE_EMAIL_DIR must be an absolute path (not ~)
   cat > .env << 'EOF'
   EMBEDDING_MODEL=BGE-base-en-v1.5
   MAILMATE_EMAIL_DIR=/Users/yourusername/Library/Application Support/MailMate/Messages
   CHROMADB_PATH=./data/chromadb
   MODEL_CACHE_DIR=./data/models
   BATCH_SIZE=32
   SEARCH_RESULTS=10
   EOF
   ```
   **Important**: Replace `/Users/yourusername` with your actual home directory path. The `MAILMATE_EMAIL_DIR` must be an absolute path for Docker volume mounting.

3. **Edit `.env` file:**
   - Set `MAILMATE_EMAIL_DIR` to your MailMate messages directory
   - Adjust other settings as needed

4. **Build the container image:**
   ```bash
   docker compose build
   ```

5. **Index your emails:**
   ```bash
   docker compose run --rm mailmate-search index
   ```
   This may take several hours for large email collections (35GB = ~700k emails). Indexing shows percent complete by default.

6. **Search your emails:**
   ```bash
   docker compose run --rm mailmate-search search "your query here"
   ```

Because this project is a CLI container (not a long-running daemon), `docker compose up -d` will start, print help, and exit. Use `docker compose run --rm ...` for commands.

## Configuration

All configuration is done via the `.env` file:

- `EMBEDDING_MODEL`: Embedding model to use (default: `BGE-base-en-v1.5`)
  - Options: `BGE-base-en-v1.5` (best quality), `BGE-small-en-v1.5`, `nomic-embed-text-v1`, `all-MiniLM-L6-v2`
- `MAILMATE_EMAIL_DIR`: Path to MailMate messages directory
- `CHROMADB_PATH`: Where to store ChromaDB data
- `MODEL_CACHE_DIR`: Where to cache downloaded models
- `BATCH_SIZE`: Number of emails to process at once (default: 32)
- `SEARCH_RESULTS`: Number of results to return (default: 10)
- `QUERY_PARSER_ENABLED`: Enable local LLM query-to-filter parsing (default: `false`)
- `QUERY_PARSER_ENDPOINT`: Local parser endpoint (default: `http://localhost:11434/api/generate`)
- `QUERY_PARSER_MODEL`: Local parser model name (default: `llama3.1:8b`)
- `QUERY_PARSER_TIMEOUT_SECONDS`: Timeout for parser call in seconds (default: `8`)
- `RERANK_ENABLED`: Enable local cross-encoder reranking (default: `false`)
- `RERANKER_MODEL`: Cross-encoder model (default: `cross-encoder/ms-marco-MiniLM-L-6-v2`)
- `RERANK_MAX_CANDIDATES`: Candidate pool size before rerank (default: `50`)
- `LOG_PATH`: Runtime log file for internal warnings/errors/diagnostics (default: `./data/logs/mailmate-search.error.log`)
- `LOG_LEVEL`: App log verbosity written to `LOG_PATH` (default: `INFO`)
- `LOG_THIRD_PARTY_LEVEL`: Third-party library log verbosity written to `LOG_PATH` (default: `WARNING`)
- `LOG_MAX_BYTES`: Log rotation size threshold in bytes (default: `10485760`)
- `LOG_BACKUP_COUNT`: Number of rotated log files to retain (default: `5`)

Normal CLI output stays in the terminal. Internal warnings, diagnostics, and traceback dumps are written to `LOG_PATH`.

## Commands

- `index`: Index emails from MailMate directory
  - `--limit N`: Limit indexing to N emails (for testing)
  - `--no-skip`: Re-index all emails even if already indexed (full rebuild behavior)
  - `--incremental`: Only scan files newer than latest indexed email date

Incremental behavior (`index --incremental`):
- Finds the most recent indexed email date and scans only files newer than that cutoff.
- Still writes updates by file path and vector ID, so reruns stay idempotent for changed files in the candidate set.

- `search "query"`: Search for emails matching the query
  - `--auto-filters/--no-auto-filters`: Override local parser toggle per query
  - `--rerank/--no-rerank`: Override local reranker toggle per query

- `status`: Show indexing status and statistics

## Storage Requirements

For 35GB of email data (~700,000 emails):
- Embeddings: ~2.1GB (with BGE-base-en-v1.5)
- Model files: ~420MB
- Metadata: ~100-200MB
- **Total**: ~2.5GB additional storage

## Model Switching

To switch embedding models:
1. Update `EMBEDDING_MODEL` in `.env`
2. Re-run indexing: `docker compose run --rm mailmate-search index --no-skip`

The system will automatically download and use the new model.

## Troubleshooting

**MailMate directory not found:**
- Check that `MAILMATE_EMAIL_DIR` in `.env` points to the correct path
- Default location: `~/Library/Application Support/MailMate/Messages`

**Out of memory:**
- Reduce `BATCH_SIZE` in `.env`
- Use a smaller model like `BGE-small-en-v1.5`

**Slow indexing:**
- This is normal for large collections (several hours for 35GB)
- The process can be interrupted and resumed
- Already indexed emails are skipped on subsequent runs
- Use `--no-skip` when you want a full rebuild across all files

**Warnings/errors while indexing:**
- Progress bars and high-level status updates stay in the terminal
- Internal warnings, diagnostics, and traceback dumps go to `LOG_PATH`

## License

MIT

