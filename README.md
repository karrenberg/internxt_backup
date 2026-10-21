# internxt_backup

Backup/sync script that uses the Internxt cloud storage CLI.

## About

This script uses the Internxt CLI interface to back up or sync local files to an Internxt cloud storage account.

It is intended to be a comfortable, fully featured, automated, and robust backup utility. Internxt can have connectivity issues, and its web client can be unreliable when uploading many files without useful progress feedback.

## Features

- Creates the same folder structure in cloud storage that exists locally, except for subfolders containing a `.internxtignore` file. A `.internxtignore` in the source root is treated as an ordinary file.
- Skips a remote file when its literal name and size match the local file. This is a size-based check; equal size does not prove equal contents.
- Copies files that do not exist remotely.
- Optionally (`--allow-delete`, `-d`) removes remote-only files/folders and remote folders corresponding to ignored local subfolders.
- Optionally (`--allow-delete`, `-d`) replaces different-size remote files using `upload-file --overwrite`, without deleting the existing file first.
- With `--allow-delete`, preserves a uniquely matched renamed folder by renaming its existing remote UUID. Matching uses names and exact file sizes; ambiguous or incomplete matches are not treated as renames.
- Prescan is enabled by default. It builds the complete action plan and exact upload totals up front, then reuses the remote listings during execution. `--no-prescan` starts folder-by-folder work sooner, while still listing existing remote folders.
- Uses up to two upload workers within a single backup run by default. Only independent first upload attempts run in parallel; set `--upload-workers 1` for sequential uploads. Listings, retries, reconciliation, folder changes, and deletion remain sequential. Parallel uploads fall back to sequential execution when token-refresh timing is uncertain.
- Retries transient CLI failures up to five total attempts by default, with bounded exponential backoff. Uncertain writes are checked against a fresh remote listing; unresolved operations make the run incomplete rather than being reported as successful.
- Shows phase, folder/path, operation age, retry wait, and upload totals when prescanning. Redirected output gets a flushed progress heartbeat at least every 15 seconds during long operations; the CLI does not expose reliable byte-level transfer progress.
- Logs CLI attempts and a final summary to a timestamped `backup_*.log`. If a run is interrupted, rerun the same command; the script rechecks remote names and sizes and skips work already present under its comparison policy.
- Returns a nonzero exit status for unresolved or incomplete work. A failed branch and its ancestors are protected from remote-only cleanup while successful independent branches can still finish.
- Requires Internxt CLI **1.6.10 or newer** for UUIDv7 folder IDs. The script checks the CLI version before remote work; CLI 1.6.10 also increased listing pagination to 1,000 items.
- Accepts files up to the CLI's 100 GiB maximum, subject to account limits. Files that exceed the limit are reported as incomplete work.
- Tested environments include Windows 11 with Node 22.18.0 and Ubuntu Server 24.04 with Node 22.23.3. Token-refresh timing for parallel uploads is verified for CLI 1.6.10; newer CLI versions run sequentially until their refresh behavior is checked.

## Usage

Requirements: Python 3.10 or newer, Internxt CLI 1.6.10 or newer, and a CLI login in the same OS user account that runs the script. Log in manually with `internxt login` when needed; the backup no longer accepts email or password arguments. Keep that session available while the backup runs; the script preserves it and does not log out.

To upgrade the CLI and check the installed version:

```bash
npm install --global @internxt/cli@latest
internxt --version
```

The reported version must be 1.6.10 or newer. Use the same Node/npm installation that provides the `internxt` command used by the backup.

The source directory is copied as a named folder under the target. The target is a Drive-root-relative folder path, an existing folder UUID, or `""` for Drive root. Add `/*` to copy only the source directory's contents directly into the target:

```bash
# Copy /backups/photos as a folder below the existing Archive folder
python internxt_backup.py --source /backups/photos --target "Archive"

# Copy the contents of /backups/photos directly into Archive/Photos
python internxt_backup.py --source "/backups/photos/*" --target "Archive/Photos"

# Copy /backups/photos as a folder directly below Drive root
python internxt_backup.py --source /backups/photos --target ""
```

### Examples

```cmd
# Windows: put the Node installation containing Internxt on PATH, verify/login, then run
set PATH=C:\Users\Me\node-v22.18.0-win-x64;%PATH%
internxt --version
# Run this only if you are not already logged in; leave the session in place
internxt login
python C:\Users\Me\internxt_backup\internxt_backup.py --source C:\Users\Me\pictures --target "" --allow-delete
```

```bash
# Linux: use the Node/npm installation that provides the installed CLI
internxt --version
# Run this only if you are not already logged in; leave the session in place
internxt login
python ~/internxt_backup/internxt_backup.py --source ~/pictures --target "" --allow-delete

# Sync a source folder below a specific parent folder UUID
python internxt_backup.py --source /path/to/2025 --target "01234567-89ab-4def-8123-456789abcdef"

# Start processing folders before the full remote plan has been built
python internxt_backup.py --source /path/to/source --target "Archive" --no-prescan

# Disable parallel uploads for this run
python internxt_backup.py --source /path/to/source --target "Archive" --upload-workers 1

# Log informational messages to the console as well as to the log file
python internxt_backup.py --source /path/to/source --target "Archive" --full-console-log

# Include each CLI attempt in the log
python internxt_backup.py --source /path/to/source --target "Archive" --verbose
```

`--allow-delete` authorizes both replacement of different-size files and deletion of remote-only or ignored items. Without it, a different-size remote file is retained and reported as a policy skip. Use the option only when that synchronization policy is intended.

### Retry, timeout, and result options

- `-r`, `--max_num_retries`, or `--max-attempts` sets the **total attempts** per CLI operation (default: 5). The legacy `--max_num_retries` name does not mean five extra attempts.
- `-w` or `--retry_wait_seconds` sets the base retry delay (default: 3 seconds). Exponential waits are capped at 60 seconds and use bounded jitter.
- Metadata commands have a 300-second whole-command deadline by default. Use `--command-timeout SECONDS` to override it.
- Upload deadlines default to `max(1800, 300 + file_size_bytes / 131072)` seconds per attempt. Use `--upload-timeout SECONDS` to override this whole-command deadline; it is not an idle timer. Large upload deadlines can cause a file to run alone when there is not enough token lifetime for a safe parallel pair.
- `--no-prescan` starts execution earlier but does not eliminate the remote listings needed to compare existing folders; exact planned upload totals are unavailable until work completes.

The exit status is 0 when the selected size-based policy completed, 1 for incomplete or unresolved work, 2 for invalid command-line input, and 130 when interrupted. Review the summary and timestamped log before treating a run as complete.

## Tests

The offline regression suite requires pytest, but no CLI installation or login:

```bash
python -m pytest -m "not integration"
```

Live integration tests are opt-in. Install the CLI, log in manually, and choose a disposable test parent folder. The tests create unique children beneath that parent, exercise upload/list/replacement/deletion with synthetic files through 20 MiB, and clean up their own children while preserving the parent and the login. No folder UUID or account credentials are supplied by the repository:

```bash
# Linux example: replace the placeholder with your disposable test parent's UUID
INTERNXT_TEST_FOLDER_ID="<disposable-folder-uuid>" python -m pytest -m integration
```

## Known Issues

- Internxt can return transient 502 responses, connection resets, and upload connection timeouts. The script retries these errors, continues independent branches, and exits nonzero if an operation remains unresolved. The log records each CLI attempt and the reconciliation listings.
- A persistent “file already exists” response that cannot be matched in a fresh listing is treated as an unresolved conflict. The script does not merge ambiguous names or claim success based only on that response.
- Legacy remote names with unusual encoding may not compare literally with local names. The script logs candidate transformations for diagnosis, but does not automatically merge or delete ambiguous entries.
- Same-name, same-size files are skipped without reading or hashing their contents. Content corruption that preserves the byte size cannot be detected by this policy.

# Internxt Resources

* CLI Reference: https://github.com/internxt/cli
* Additional information: https://help.internxt.com/en/articles/9178044-does-internxt-support-webdav
* CLI 1.6.10 release notes: https://github.com/internxt/cli/releases/tag/v1.6.10
