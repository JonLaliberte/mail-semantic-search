# Agent Notes

Working notes for AI agents and human maintainers. Everything user-facing lives in `README.md`; this file is for the bits that would otherwise clutter the README.

## Doc maintenance

- Keep `README.md` up to date when changing user-facing behavior: setup steps, CLI commands or flags, environment variables, Docker usage, scheduling guidance, troubleshooting.
- Keep `AGENTS.md` up to date when changing maintainer-facing behavior: release flow, internal tooling, migration commands, dev setup.
- Update docs in the same change that affects behavior — don't leave follow-up cleanup work.
- If behavior changed and the correct target is unclear, update both files.
- The README's pitch (the paragraph under the `# mail-semantic-search` heading) is the project's elevator pitch — it should describe *what value this delivers*, not just *what it is*. Resist letting it drift into feature-list territory.

## Development setup

Create a venv and install the package in editable mode:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install pytest  # not in default deps
```

`.env` is auto-loaded by `python-dotenv` from the current working directory, so run all commands from the project root.

Run tests:

```bash
.venv/bin/python -m pytest tests/ -q
```

Run a native invocation (mirrors what users do, much faster than Docker on macOS):

```bash
.venv/bin/mail-semantic-search index --incremental
.venv/bin/mail-semantic-search-mcp
```

### Testing the Docker image locally

`docker-compose.yml` no longer carries a `build:` directive — `docker compose pull` and `docker compose run --rm` always use the published GHCR image. When you need to test a `Dockerfile` change before tagging a release, build directly and tag it as `:latest` so compose picks it up:

```bash
docker build -t ghcr.io/jonlaliberte/mail-semantic-search:latest .
docker compose run --rm mail-semantic-search index --incremental
```

Re-pulling later restores the published image.

## Releasing a new version (maintainers)

1. Bump `version` in `pyproject.toml` following semver:
   - **Patch** for bugfixes and perf improvements with no API or layout change.
   - **Minor** for new features, new CLI commands, or changes to `docker-compose.yml` that existing users must adapt to.
   - **Major** reserved for breaking API or data-format changes that require manual migration.
2. Commit the version bump together with the change it gates (not as a separate "bump version" commit).
3. Tag and push:
   ```bash
   git tag -a v0.5.0 -m "Release 0.5.0"
   git push origin v0.5.0
   ```
4. CI (`.github/workflows/release.yml`) builds the multi-arch (linux/amd64 + linux/arm64) image via buildx + QEMU, pushes to `ghcr.io/jonlaliberte/mail-semantic-search` with `:VERSION`, `:MAJOR.MINOR`, and `:latest` tags, and drafts a GitHub Release with auto-generated notes.
5. **First release publishes the GHCR package as private** — flip it to Public once in the GitHub UI (Packages → Package settings → Change visibility). Subsequent publishes inherit that visibility.

## Extraction versioning

`CURRENT_EXTRACTION_VERSION` in `mail_semantic_search/mailmate_reader.py` is the single source of truth for "what version of the parse pipeline produced this row's `body_preview` / vectors." Every row in `emails.extraction_version` is set to this value at insert/upsert time.

**Bump the constant** whenever `parse_email_file`'s output for the same `.eml` could meaningfully change — HTML conversion changes, header changes, quoted-reply behavior changes, anything that affects `body` or `combine_email_text` output. Adding tools (e.g. a new MCP action) that don't touch existing rows does NOT require a bump.

**Every bump requires a changelog line** directly above the constant:

```
#   N — YYYY-MM-DD: <what changed and why>
```

`tests/test_extraction_version.py` is a tripwire that fails CI if the constant and changelog drift.

After a bump, run a backfill:

```bash
.venv/bin/mail-semantic-search reextract --dry-run        # count stale rows
.venv/bin/mail-semantic-search reextract --file-path "<one rep email>"   # visual QA
.venv/bin/mail-semantic-search reextract --batch-size 128 # full backfill
```

The bulk path holds a backfill lock (`app_state.backfill_in_progress`) for the duration. Concurrent `index --incremental` and `index-file` calls exit cleanly with a benign message — neither output string contains `error` or `warning` substrings (case-insensitive), so KM / launchd watchers don't pop alerts mid-backfill. The lock auto-clears if the holding PID dies.

Realistic rate on Apple Silicon: ~14 rows/sec at `--batch-size 128`. ~8h for 437k rows; ETA prints per-batch.

## Path migration tooling

`mail-semantic-search migrate-paths --old-prefix X --new-prefix Y` rewrites indexed `file_path` values across both SQLite (`emails.file_path`, `emails.file_hash`) and Chroma (document IDs are `md5(file_path)`; `metadata['file_path']` is the stored copy). It reuses existing embeddings — no re-embed cost — and is idempotent (rows whose new IDs are already in Chroma are skipped, so interrupted runs can resume).

Use when:
- The container's bind-mount layout changes (e.g. the v0.5.0 switch from `/emails` to host-path-mirroring).
- The host maildir moves to a new physical location.
- Rows accumulated under multiple path forms (symlinked + resolved) and you want to consolidate.

Always back up first — APFS clone is near-instant on the same volume:

```bash
cp -c -R /path/to/data /path/to/data.backup-$(date +%Y-%m-%d)
```

Then dry-run, then apply:

```bash
.venv/bin/mail-semantic-search migrate-paths \
  --old-prefix "/emails/" \
  --new-prefix "$EMAIL_DIR/" \
  --dry-run

.venv/bin/mail-semantic-search migrate-paths \
  --old-prefix "/emails/" \
  --new-prefix "$EMAIL_DIR/"
```

Expect ~10 minutes natively, ~30 minutes in Docker, for ~437k rows.

## Path consistency invariant

As of v0.5.0, the docker-compose mount layout is `${EMAIL_DIR}:${EMAIL_DIR}:ro` (host path identical to container path), and `EMAIL_DIR` is inherited inside the container from `.env` via `env_file`. This means:

- `find` results inside the container and natively produce the same strings.
- Skip-before-parse (`Database.get_indexed_mtime(path)`) works across Docker and native runs against the same DB.
- `file_path` values stored in SQLite and Chroma are stable regardless of which runtime indexed them.

If you ever introduce a code path that resolves symlinks or otherwise canonicalizes paths, do it consistently in *both* `scan_eml_files` and `index_email_file`, or you'll get duplicate rows under different prefixes (caught and migrated by `migrate-paths`, but ugly).

## macOS find quirks

`find` does **not** follow top-level symbolic links by default. When `EMAIL_DIR` is a symlink (a common setup for users keeping the maildir on an external SSD), a bare `find` returns zero results and the indexer silently advances the watermark, never indexing anything. Docker bind-mounts resolve the symlink at mount time, masking the bug there.

`_scan_eml_files_find` in `mailmate_reader.py` passes `-L` to `find` to handle this. Don't remove that flag without a replacement.
