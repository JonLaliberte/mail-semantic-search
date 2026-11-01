# Installation

There are two ways to run mail-semantic-search. Both read the same `.env` and can share one index, so you can switch between them freely.

| | Native (Python venv) | Docker |
|---|---|---|
| Needs | Python 3.10+ | Docker Desktop |
| Incremental index run | ~10–30 seconds | ~2 minutes (Docker's macOS filesystem layer is slower) |
| MCP server | Runs directly | Runs inside a container ([MCP via Docker](mcp.md#option-b-mcp-via-docker)) |
| Best for | Most people, scheduled runs | Machines without a Python toolchain |

## Requirements

- macOS or Linux (developed and tested on macOS). The MailMate actions (`open_email`, `archive_email` and so on) are macOS only.
- An email client that stores messages as `.eml` files. MailMate is the main target; inbox listing and deep links assume MailMate's folder layout.
- 16 GB+ RAM recommended. 8 GB may work for small archives. Apple Silicon or modern x86_64. Tested on a Mac Mini M4 with 24 GB.
- Disk for the index: roughly **2.5 GB per 35 GB of email** (~700k messages), split across embeddings (~2.1 GB), model files (~420 MB) and metadata (~100–200 MB).

## Native

```bash
git clone https://github.com/JonLaliberte/mail-semantic-search.git
cd mail-semantic-search
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

Edit `.env` and set `EMAIL_DIR` to your messages folder. It must be an absolute path (`/Users/you/...`, not `~/...`). The MailMate default is:

```
EMAIL_DIR=/Users/you/Library/Application Support/MailMate/Messages
```

`.env` is loaded from the current working directory, so always run commands from the project folder (or `cd` into it first, as the MCP and scheduling examples do).

Build the index:

```bash
.venv/bin/mail-semantic-search index
```

The first run downloads the embedding model into `MODEL_CACHE_DIR` and needs access to `huggingface.co`. After that, models load from the local cache and indexing works offline. A full index of a large archive (35 GB, ~700k messages) takes several hours. The run shows percent complete, can be interrupted, and skips already-indexed messages when restarted.

To try it on a small sample first:

```bash
.venv/bin/mail-semantic-search index --limit 500
.venv/bin/mail-semantic-search search "quarterly planning deck"
.venv/bin/mail-semantic-search status
```

If you see `Operation not permitted` while reading mail, give your terminal app Full Disk Access (System Settings → Privacy & Security → Full Disk Access). This is common when the maildir is on an external drive.

### Upgrading

```bash
git pull
.venv/bin/pip install -e .
```

Check the [release notes](https://github.com/JonLaliberte/mail-semantic-search/releases) for anything that needs a one-off command after upgrading.

## Docker

You still need the repo (or at least `docker-compose.yml` and a `.env`), because Compose reads both from the current folder.

```bash
git clone https://github.com/JonLaliberte/mail-semantic-search.git
cd mail-semantic-search
cp .env.example .env    # set EMAIL_DIR (absolute path)
docker compose pull
docker compose run --rm mail-semantic-search index
docker compose run --rm mail-semantic-search search "your query here"
```

The container mounts `EMAIL_DIR` read-only at the same path it has on the host, and stores the index under `DATA_VOLUME_PATH` (default `./data`). Because paths match, an index built in Docker works natively and vice versa.

This is a CLI container, not a long-running service. `docker compose up -d` starts it, prints help and exits. Use `docker compose run --rm ...` for every command.

To keep the index on an external drive, set `DATA_VOLUME_PATH` to an absolute path such as `/Volumes/My Drive/mail-semantic-search/data`.

### Image tags

Releases are published as multi-arch (amd64 + arm64) images to GitHub Container Registry:

```
ghcr.io/jonlaliberte/mail-semantic-search
```

- `:latest`: most recent release
- `:0.5`, `:0.5.0`: minor and exact-version aliases (substitute the current release)

To pin a version, edit the `image:` line in `docker-compose.yml`:

```yaml
image: ghcr.io/jonlaliberte/mail-semantic-search:0.5.0
```

Upgrade with `docker compose pull`.

## Versions

Releases are cut automatically from merged pull requests. `mail-semantic-search --version` and `status` report the installed version. Release notes are on the [GitHub Releases page](https://github.com/JonLaliberte/mail-semantic-search/releases).

## Next steps

- [Connect the MCP server](mcp.md) to Claude or another AI client
- [Schedule incremental indexing](scheduling.md)
- [Tune the configuration](configuration.md)
