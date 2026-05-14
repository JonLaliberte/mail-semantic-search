# Design: Prepare mailmate-search for Public Release

**Date:** 2026-05-14  
**Scope:** Minimal cleanup + light README polish (Option B)  
**Goal:** Remove personal/machine-specific content, add missing legal and onboarding files, and document scheduling — so any stranger can clone, configure, and run the project without confusion.

---

## What Changes

### 1. `docker-compose.yml` — replace hardcoded volume path

**Problem:** The data volume is hardcoded to `/Volumes/External Storage SSD/Docker/mailmate-search/data:/app/data`, which is specific to the author's machine and will silently break for anyone else.

**Fix:** Replace with an env-var-backed default:

```yaml
- "${DATA_VOLUME_PATH:-./data}:/app/data"
```

`DATA_VOLUME_PATH` defaults to `./data` (works out of the box for local storage). Users with data on an external drive set it in `.env`. This follows the same pattern already used for `MAILMATE_EMAIL_DIR`.

---

### 2. `.env.example` — new file

A committed template users copy to `.env` on first setup. Includes all documented variables with placeholder values and brief inline comments. Adds `DATA_VOLUME_PATH` as a new entry.

```
EMBEDDING_MODEL=BGE-base-en-v1.5
MAILMATE_EMAIL_DIR=/Users/yourusername/Library/Application Support/MailMate/Messages

# Where Docker stores the index data (chromadb, sqlite, models, logs).
# Set to an absolute path if storing on an external drive.
DATA_VOLUME_PATH=./data

CHROMADB_PATH=/app/data/chromadb
MODEL_CACHE_DIR=/app/data/models
BATCH_SIZE=32
SEARCH_RESULTS=10
```

---

### 3. `LICENSE` — add MIT license file

The README already declares MIT. This adds the actual `LICENSE` file so GitHub renders the license badge and the project is legally complete.

---

### 4. `README.md` — targeted updates

#### Hardware requirements

Replace:
> Mac Mini M4 with 24GB RAM (or similar hardware)

With:
> **Tested on:** Mac Mini M4, 24GB RAM  
> **Minimum recommended:** 16GB RAM (8GB may work for small email collections); Apple Silicon or modern x86_64; Docker Desktop

#### Configuration table

Add `DATA_VOLUME_PATH` entry:
> `DATA_VOLUME_PATH`: Host path Docker bind-mounts as `/app/data` (default: `./data`). Set to an absolute path for external drive storage.

#### MCP section cleanup

Remove the dangling implementation note at the bottom of the MCP section:
> "Phase 3 answer synthesis should stay separate from retrieval..."

This is an internal dev note, not user-facing documentation.

#### New section: "Keeping Your Index Fresh"

Placed after the MCP section, before Storage Requirements. Content:

**Why:** The MCP server and CLI search only what has been indexed. Running `index --incremental` regularly keeps results current as new mail arrives. Once a day is the minimum; more frequent runs (e.g. every few hours) keep it tighter.

**The command to schedule:**
```bash
cd /Users/yourusername/Development/mailmate-search && /opt/homebrew/bin/docker compose run --rm mailmate-search index --incremental
```

Note: Use the full path to `docker` (`/opt/homebrew/bin/docker` on Apple Silicon Homebrew installs) since cron and launchd don't inherit your shell `PATH`.

Three scheduling options are documented:

**Option A — cron (quick):**
```
0 * * * * cd /Users/yourusername/Development/mailmate-search && /opt/homebrew/bin/docker compose run --rm mailmate-search index --incremental >> /tmp/mailmate-index.log 2>&1
```
Edit with `crontab -e`. This example runs hourly; change `0 * * * *` to `0 9 * * *` for once daily at 9am.

**Option B — launchd (robust, macOS-native):**

A `.plist` saved to `~/Library/LaunchAgents/com.mailmate-search.index.plist` and loaded with `launchctl load`. Keys: `Label`, `ProgramArguments` (using `/bin/zsh -lc <command>`), `StartCalendarInterval` (e.g. `{Hour: 9, Minute: 0}` for 9am daily), `StandardOutPath`, `StandardErrorPath`, `RunAtLoad: false`. Load with `launchctl load ~/Library/LaunchAgents/com.mailmate-search.index.plist`.

**Option C — Keyboard Maestro:**

For users who already have Keyboard Maestro: create a macro with a "Time of Day" (or "Periodic") trigger and an "Execute Shell Script" action. Set the shell to `/bin/zsh` and paste the full command. No PATH issues since the full docker path is explicit.

---

## Files Changed

| File | Action |
|------|--------|
| `docker-compose.yml` | Replace hardcoded volume path with `${DATA_VOLUME_PATH:-./data}` |
| `.env.example` | Create new file |
| `LICENSE` | Create new MIT license file |
| `README.md` | Hardware req update, add DATA_VOLUME_PATH to config table, remove Phase 3 note, add "Keeping Your Index Fresh" section |

---

## Out of Scope

- GitHub Actions CI
- Issue / PR templates
- CONTRIBUTING.md
- Any code changes
