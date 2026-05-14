# Public Repo Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove personal/machine-specific content, add missing legal and onboarding files, and document scheduling so the repo is clean for public release.

**Architecture:** Pure documentation and configuration changes — no Python code touched. Four files changed: `docker-compose.yml` (env-var volume path), `.env.example` (new onboarding template), `LICENSE` (MIT), `README.md` (hardware requirements, config table, scheduling section, remove internal note).

**Tech Stack:** Docker Compose, bash/zsh, launchd, Keyboard Maestro

---

### Task 1: Fix hardcoded volume path in `docker-compose.yml`

**Files:**
- Modify: `docker-compose.yml:7`

- [ ] **Step 1: Replace the hardcoded volume line**

Open `docker-compose.yml`. Change line 7 from:
```yaml
      - "/Volumes/External Storage SSD/Docker/mailmate-search/data:/app/data"
```
To:
```yaml
      - "${DATA_VOLUME_PATH:-./data}:/app/data"
```

The full file should look like:
```yaml
services:
  mailmate-search:
    build: .
    container_name: mailmate-search
    volumes:
      - ${MAILMATE_EMAIL_DIR}:/emails:ro
      - "${DATA_VOLUME_PATH:-./data}:/app/data"
    environment:
      - EMBEDDING_MODEL=${EMBEDDING_MODEL:-BGE-base-en-v1.5}
      - MAILMATE_EMAIL_DIR=/emails
      - CHROMADB_PATH=/app/data/chromadb
      - MODEL_CACHE_DIR=/app/data/models
      - DATABASE_PATH=/app/data/database.db
      - BATCH_SIZE=${BATCH_SIZE:-32}
      - SEARCH_RESULTS=${SEARCH_RESULTS:-10}
    env_file:
      - .env
    stdin_open: true
    tty: true
```

- [ ] **Step 2: Verify Docker Compose parses it cleanly**

```bash
docker compose config
```

Expected: Compose prints the resolved config with `./data:/app/data` (since `DATA_VOLUME_PATH` is not set in your shell). No errors.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "fix: replace hardcoded data volume path with DATA_VOLUME_PATH env var"
```

---

### Task 2: Create `.env.example`

**Files:**
- Create: `.env.example`

- [ ] **Step 1: Create the file**

Create `.env.example` at the repo root with this exact content:

```
# Copy this file to .env and fill in your values.
# MAILMATE_EMAIL_DIR and DATA_VOLUME_PATH must be absolute paths.

EMBEDDING_MODEL=BGE-base-en-v1.5

# Absolute path to your MailMate messages directory.
MAILMATE_EMAIL_DIR=/Users/yourusername/Library/Application Support/MailMate/Messages

# Where Docker stores the index data (chromadb, sqlite, models, logs).
# Defaults to ./data (local). Set to an absolute path for external drive storage,
# e.g. DATA_VOLUME_PATH=/Volumes/My Drive/mailmate-search/data
DATA_VOLUME_PATH=./data

BATCH_SIZE=32
SEARCH_RESULTS=10

# --- Optional: local LLM query parsing (default: off) ---
# QUERY_PARSER_ENABLED=false
# QUERY_PARSER_ENDPOINT=http://localhost:11434/api/generate
# QUERY_PARSER_MODEL=llama3.1:8b
# QUERY_PARSER_TIMEOUT_SECONDS=8

# --- Optional: cross-encoder reranking (default: off) ---
# RERANK_ENABLED=false
# RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
# RERANK_MAX_CANDIDATES=50
# RERANK_MAX_TEXT_CHARS=1200

# --- Optional: logging ---
# LOG_PATH=./data/logs/mailmate-search.error.log
# LOG_LEVEL=INFO
# LOG_THIRD_PARTY_LEVEL=WARNING
```

- [ ] **Step 2: Verify it is not gitignored**

```bash
git check-ignore -v .env.example
```

Expected: no output (meaning it is not ignored). If it prints anything, inspect `.gitignore` — the pattern `/.env` ignores only the exact file `.env`, so `.env.example` should be fine.

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "add .env.example onboarding template"
```

---

### Task 3: Add `LICENSE` file

**Files:**
- Create: `LICENSE`

- [ ] **Step 1: Create the MIT license file**

Create `LICENSE` at the repo root with this exact content (year 2026, author Jonathan Laliberte):

```
MIT License

Copyright (c) 2026 Jonathan Laliberte

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 2: Commit**

```bash
git add LICENSE
git commit -m "add MIT license"
```

---

### Task 4: Update `README.md` — requirements, config table, Quick Start, remove internal note

**Files:**
- Modify: `README.md`

#### Step 1: Update the Requirements section

- [ ] Replace:
```markdown
- Docker and Docker Compose
- Mac Mini M4 with 24GB RAM (or similar hardware)
- MailMate email client with emails stored as .eml files
```
With:
```markdown
- Docker and Docker Compose
- MailMate email client with emails stored as .eml files
- **Tested on:** Mac Mini M4, 24GB RAM
- **Minimum recommended:** 16GB RAM (8GB may work for small collections); Apple Silicon or modern x86_64
```

#### Step 2: Update the Quick Start `.env` creation example

- [ ] In the Quick Start step 2 `cat > .env` block, replace:
```
   EMBEDDING_MODEL=BGE-base-en-v1.5
   MAILMATE_EMAIL_DIR=/Users/yourusername/Library/Application Support/MailMate/Messages
   CHROMADB_PATH=./data/chromadb
   MODEL_CACHE_DIR=./data/models
   BATCH_SIZE=32
   SEARCH_RESULTS=10
```
With:
```
   EMBEDDING_MODEL=BGE-base-en-v1.5
   MAILMATE_EMAIL_DIR=/Users/yourusername/Library/Application Support/MailMate/Messages
   DATA_VOLUME_PATH=./data
   BATCH_SIZE=32
   SEARCH_RESULTS=10
```

And update the tip below that block to mention `.env.example`:

- [ ] After the `**Important**: Replace ...` sentence, add:
```markdown
   Alternatively, copy `.env.example` to `.env` and edit it.
```

#### Step 3: Add `DATA_VOLUME_PATH` to the Configuration section

- [ ] In the Configuration list, after the `MAILMATE_EMAIL_DIR` bullet, insert:
```markdown
- `DATA_VOLUME_PATH`: Host path Docker bind-mounts as `/app/data` (default: `./data`). Set to an absolute path when storing index data on an external drive, e.g. `/Volumes/My Drive/mailmate-search/data`
```

#### Step 4: Remove the dangling Phase 3 internal note

- [ ] Delete this line from the MCP section (it appears just after the Claude Desktop notes block):
```
Phase 3 answer synthesis should stay separate from retrieval, either as a future MCP tool like `answer_question` or a separate CLI command.
```

- [ ] **Commit all README changes so far**

```bash
git add README.md
git commit -m "docs: update hardware requirements, add DATA_VOLUME_PATH to config, clean up MCP note"
```

---

### Task 5: Add "Keeping Your Index Fresh" section to `README.md`

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Insert the new section**

Insert the following section between the end of the MCP section and `## Storage Requirements`:

````markdown
## Keeping Your Index Fresh

The MCP server and CLI search only what has been indexed. Running `index --incremental` regularly keeps results current as new mail arrives. Once a day is the minimum; every few hours is better.

**Command to schedule** (replace `yourusername` with your home directory):

```bash
cd /Users/yourusername/Development/mailmate-search && /opt/homebrew/bin/docker compose run --rm mailmate-search index --incremental
```

> **Note:** Use the full path to `docker` (`/opt/homebrew/bin/docker` on Apple Silicon Homebrew installs). Cron and launchd do not inherit your shell `PATH`.

### Option A — cron (quick)

Edit your crontab with `crontab -e` and add one line. This example runs every hour:

```
0 * * * * cd /Users/yourusername/Development/mailmate-search && /opt/homebrew/bin/docker compose run --rm mailmate-search index --incremental >> /tmp/mailmate-index.log 2>&1
```

To run once daily at 9am instead, change the schedule to `0 9 * * *`.

### Option B — launchd (robust, macOS-native)

Create `~/Library/LaunchAgents/com.mailmate-search.index.plist` with the following content (replace `yourusername`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mailmate-search.index</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-lc</string>
        <string>cd /Users/yourusername/Development/mailmate-search &amp;&amp; /opt/homebrew/bin/docker compose run --rm mailmate-search index --incremental</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/mailmate-search-index.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mailmate-search-index.error.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.mailmate-search.index.plist
```

To unload: `launchctl unload ~/Library/LaunchAgents/com.mailmate-search.index.plist`

### Option C — Keyboard Maestro

For users who already have [Keyboard Maestro](https://www.keyboardmaestro.com/):

1. Create a new macro with a **Time of Day** trigger (or **Periodic** trigger for sub-hourly).
2. Add an **Execute Shell Script** action. Set shell to `/bin/zsh`.
3. Paste the full command:
   ```
   cd /Users/yourusername/Development/mailmate-search && /opt/homebrew/bin/docker compose run --rm mailmate-search index --incremental
   ```
4. Optionally pipe output to a log file by appending `>> /tmp/mailmate-index.log 2>&1`.

No PATH issues since the full docker path is explicit.
````

- [ ] **Step 2: Verify the section is in the right place**

Open `README.md` and confirm the new section appears between the Claude Desktop notes and `## Storage Requirements`.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add Keeping Your Index Fresh scheduling section (cron, launchd, Keyboard Maestro)"
```

---

## Spec Coverage Check

| Spec requirement | Task |
|-----------------|------|
| Replace hardcoded docker-compose volume path | Task 1 |
| Add `.env.example` with DATA_VOLUME_PATH | Task 2 |
| Add `LICENSE` (MIT) | Task 3 |
| Hardware requirements — tested + minimum | Task 4, Step 1 |
| DATA_VOLUME_PATH in config table | Task 4, Step 3 |
| Quick Start updated with DATA_VOLUME_PATH | Task 4, Step 2 |
| Remove Phase 3 internal note | Task 4, Step 4 |
| "Keeping Your Index Fresh" with cron, launchd, KM | Task 5 |
