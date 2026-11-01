# Keeping the index fresh

Search only covers mail that has been indexed. Run `index --incremental` on a schedule so new mail shows up: daily at minimum, every hour or few hours for best results. The MCP server picks up each run automatically, so there's nothing to restart.

## Pick a command

Both commands write identical file paths into the index, so you can mix them.

**Native (recommended):** about 10–30 seconds per run.

```bash
cd /Users/you/mail-semantic-search && .venv/bin/mail-semantic-search index --incremental
```

**Docker:** about 2 minutes per run, because Docker's macOS filesystem layer is slower.

```bash
cd /Users/you/mail-semantic-search && /opt/homebrew/bin/docker compose run --rm mail-semantic-search index --incremental
```

Use full paths in scheduled jobs. cron and launchd don't inherit your shell's `PATH`; on Apple Silicon Homebrew installs, `docker` is at `/opt/homebrew/bin/docker`.

In the examples below, replace `/Users/you/mail-semantic-search` with your project folder, and swap in the Docker command if that's what you use.

## launchd (macOS, recommended)

Create `~/Library/LaunchAgents/com.mail-semantic-search.index.plist`. This one runs every hour:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mail-semantic-search.index</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-lc</string>
        <string>cd /Users/you/mail-semantic-search &amp;&amp; .venv/bin/mail-semantic-search index --incremental</string>
    </array>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>/tmp/mail-semantic-search-index.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mail-semantic-search-index.error.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

For once a day at 9am instead, replace the `StartInterval` key and value with:

```xml
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>9</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.mail-semantic-search.index.plist
```

Unload it with `launchctl unload` and the same path.

If runs fail with `Operation not permitted`, the job can't read your maildir. Give Full Disk Access to `/bin/zsh`, or use Keyboard Maestro (below) if it already has access.

## cron

Run `crontab -e` and add one line. This runs at the top of every hour:

```
0 * * * * cd /Users/you/mail-semantic-search && .venv/bin/mail-semantic-search index --incremental >> /tmp/mail-semantic-search-index.log 2>&1
```

Use `0 9 * * *` for once a day at 9am.

## Keyboard Maestro

1. Create a macro with a **Time of Day** trigger, or a **Periodic** trigger for intervals under a day.
2. Add an **Execute Shell Script** action with the shell set to `/bin/zsh`.
3. Paste the command:
   ```
   cd /Users/you/mail-semantic-search && .venv/bin/mail-semantic-search index --incremental >> /tmp/mail-semantic-search-index.log 2>&1
   ```

## Index new mail instantly

For mail that should be searchable the moment it arrives, have a MailMate rule run `index-file` on each new message (see the [CLI reference](cli.md#index-file-path)). Keep the scheduled incremental run as well, to catch moves, archives and anything a rule misses.

## Housekeeping

Every so often, especially after deleting or moving lots of mail:

```bash
.venv/bin/mail-semantic-search prune --dry-run      # then without --dry-run
.venv/bin/mail-semantic-search compact-vectors --dry-run
```

`prune` drops index entries for mail that no longer exists. Run `compact-vectors` only if its dry run shows the index is much bigger than expected; see the [CLI reference](cli.md#compact-vectors).
