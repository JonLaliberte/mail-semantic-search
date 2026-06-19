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

## Commit & PR conventions (READ THIS — agents included)

Releases are **fully automatic** and driven by [Conventional Commits](https://www.conventionalcommits.org/). **Never bump a version by hand** — there is no static `version` in `pyproject.toml` (it's derived from git tags by setuptools-scm).

PRs are **squash-merged using the PR title as the commit subject**, so the one rule that matters:

> **Every PR title MUST be a Conventional Commit:** `type: subject`
> (e.g. `feat: add prune command`, `fix: handle empty mailbox`).

CI enforces it — the `PR Title` workflow (`.github/workflows/pr-title.yml`) fails any PR whose title isn't valid. A non-conventional title would silently skip the release and leave the next deploy unversioned, which is exactly what this setup prevents.

Title prefix → version bump (computed by python-semantic-release):

| Prefix | Bump | Example |
|---|---|---|
| `fix:`, `perf:` | patch | `0.9.0 → 0.9.1` |
| `feat:` | minor | `0.9.0 → 0.10.0` |
| `feat!:` / `BREAKING CHANGE:` footer | minor while < 1.0.0 (`major_on_zero=false`) | `0.9.0 → 0.10.0` |
| `chore:`, `docs:`, `refactor:`, `test:`, `ci:`, `build:`, `style:` | no release | — |

If a change should ship but the natural type is no-release, title the PR `feat:`/`fix:` so a version is actually cut. While pre-1.0, breaking changes bump the minor; promote to `1.0.0` with a manual tag when ready.

## Releasing a new version (maintainers)

Releases happen on their own:

1. Merge a PR to `main` with a Conventional Commit title (see above).
2. On push to `main`, `.github/workflows/release.yml` runs `python-semantic-release` in **tag-only** mode (`commit: false`): it computes the next semver from the commits since the last `v*` tag and, if anything is releasable, creates the `vX.Y.Z` tag and a GitHub Release. Nothing is committed back to `main` (so it works with branch protection — no PAT needed).
3. A dependent `docker` job builds the multi-arch (amd64+arm64) image, stamps it with `APP_VERSION`/`GIT_SHA` build-args, and pushes to `ghcr.io/jonlaliberte/mail-semantic-search` with `:VERSION`, `:MAJOR.MINOR`, and `:latest` tags.

Runtime version resolution lives in `mail_semantic_search/version.py`: `APP_VERSION` env → installed package metadata (filled by setuptools-scm from the tag) → `0.0.0.dev0` fallback. So `--version`/`status` always report a version, and nothing is ever unversioned.

**One-time repo settings (GitHub UI — can't be set from files):**
- Settings → General → Pull Requests → "Default commit message" for squash merge = **"Pull request title"** (so the squash subject is the Conventional Commit the release reads).
- **First GHCR publish is private** — flip the package to Public once (Packages → Package settings → Change visibility). Later publishes inherit it.

To cut a release without a new merge (or re-run after a hiccup), trigger the `Release` workflow via **workflow_dispatch**.

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
