#!/usr/bin/env python3

"""Copy one local file or directory into a named Internxt Drive folder."""

from __future__ import annotations

import argparse
import signal
import threading
import time
from pathlib import Path
from typing import Any

from internxt_client import (
    AuthenticationError, BackupError, BranchError, CancelledError, CliRunner,
    DEFAULT_COMMAND_TIMEOUT, DEFAULT_UPLOAD_TIMEOUT, FILE_SIZE_UPLOAD_LIMIT_BYTES,
    InternxtClient, LocalDirectory, LocalFile, Progress,
    configure_logging, format_size, local_directory_unchanged, positive_int,
    positive_number, resolve_cli, scan_local_tree,
)


# Keep the complete usage guide in the parser so --help works without a CLI installation or login.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="One local file, or a directory with -r; quote paths containing spaces")
    parser.add_argument("-t", "--target", required=True,
                        help="Existing absolute Drive folder path, e.g. /Archive/Photos/; / is Drive root")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Copy the named directory and all its contents")
    parser.add_argument("--max-attempts", type=positive_int, default=5,
                        help="Maximum total attempts per CLI operation, including the first (default: 5)")
    parser.add_argument("-w", "--retry-wait-seconds", type=positive_int, default=3, metavar="SECONDS",
                        help="Base retry delay in seconds (default: 3; exponential waits capped at 60)")
    parser.add_argument("--command-timeout", type=positive_number, default=DEFAULT_COMMAND_TIMEOUT, metavar="SECONDS",
                        help="Whole-command deadline per metadata attempt (default: 300 seconds)")
    parser.add_argument("--upload-timeout", type=positive_number, default=DEFAULT_UPLOAD_TIMEOUT, metavar="SECONDS",
                        help="Whole-command deadline per upload attempt; default: max(1800, 300 + size_bytes / 131072)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Include individual CLI attempts in the log file")
    parser.epilog = """Examples:
  internxtcp path/to/myfile -t /path/to/
      Copies myfile to /path/to/myfile.

  internxtcp -r path/to/mydir -t /path/to/other/dir/
      Copies mydir and its contents to /path/to/other/dir/mydir/.

  internxtcp "my photo.jpg" -t "/Family Photos/2026/"
      Quote local and remote paths containing spaces or shell metacharacters.

  internxtcp myfile -t /
      Copies myfile directly into Drive root.

  python internxtcp.py myfile -t /Archive/ --max-attempts 8 --upload-timeout 3600
      Runs without a launcher, with up to 8 attempts and a 1-hour upload deadline.

Destination and copy behavior:
  - Target paths start with / and are absolute from Internxt Drive root.
    Use literal folder names with / separators, without a remote prefix or UUID.
    Names are matched exactly; UUID-looking names are treated as folder names.
    A trailing / is optional. The . and .. path components are not accepted.
  - The target always names an existing destination folder, not a new filename.
    Missing target folders are errors; create them before running this command.
  - A directory requires -r. Its own name is included, even with a trailing
    local separator. Missing folders inside the copied tree are created;
    an existing same-name directory is merged with the local directory.
  - Matching remote filenames are overwritten, even when sizes match.
    Remote-only items are kept. File/directory name collisions are errors.
  - .internxtignore does not exclude anything: marked directories and the
    marker files themselves are copied. Files over 100 GiB are errors.

Retries and results:
  Transient CLI failures use bounded exponential backoff with jitter, capped
  at 60 seconds. Permanent errors and authentication failures are not retried.
  Uncertain writes are checked against fresh remote listings before retrying.
  Recovery uses exact names and sizes, not content hashes. An existing equal-size
  file cannot confirm an overwrite: that case requires an upload success response
  and retries the authorized overwrite if its outcome is uncertain.

  Timeouts apply to a whole command attempt, not to idle time. Retry waits and
  reconciliation listings can make the overall run longer than one timeout.
  Failed items make the run incomplete; independent items can still finish.

Setup and use outside this repository:
  Requires Python 3.10+ and Internxt CLI 1.6.10+ on PATH. First log in manually
  with `internxt login` as the same OS user. The existing login is preserved.
  Keep internxtcp.py and internxt_client.py together; the backup script is not
  required. Run `python /path/to/internxtcp.py ...`, or add their folder to PATH
  and use the internxtcp.cmd (Windows) or internxtcp (Linux/macOS) launcher.

Logging and exit status:
  Diagnostics and a summary are written to internxtcp_*.log in the current
  working directory. Warnings/errors also appear on the console. --verbose
  includes each CLI attempt in the log. Review errors before treating a copy
  as complete.

  0  Copy completed.
  1  Incomplete or unresolved work, including CLI, login, or transfer failures.
  2  Invalid command-line input, including a missing source or missing -r.
  130  Interrupted.
"""
    return parser


# Resolve literal components from root; UUID-looking names have no special meaning here.
def target_parts(target: str) -> list[str]:
    if not target.startswith("/"):
        raise BackupError("Target must be an absolute Drive folder path starting with /.")
    parts = [part for part in target.split("/") if part]
    if any(part in (".", "..") for part in parts):
        raise BackupError("Target must name an absolute folder without . or .. components.")
    return parts


# Copy policy only selects paths and replacement behavior; all CLI recovery stays in the client.
class Copy(InternxtClient):
    def fail(self, message: str) -> None:
        self.stats.failures.append(message)
        self.logger.error("%s", message)

    def resolve_target(self, parts: list[str]) -> str:
        current = ""
        walked = ""
        for part in parts:
            listing = self.list_remote(current, walked or "/")
            walked += "/" + part
            item = listing.get(part)
            if item is None or item.kind != "folder":
                raise BranchError(f"Target folder does not exist or is blocked by a file: {walked}")
            current = item.uuid
        return current

    def copy_file(self, file: LocalFile, parent_uuid: str, remote_path: str) -> None:
        try:
            if self.cancel_event.is_set():
                raise CancelledError("Interrupted before copying the next file.")
            if file.size > FILE_SIZE_UPLOAD_LIMIT_BYTES:
                self.stats.files_too_large += 1
                raise BranchError(f"File exceeds the Internxt 100 GiB limit: {file.path}")
            listing = self.list_remote(parent_uuid, remote_path)
            existing = listing.get(file.name)
            if existing is not None and existing.kind != "file":
                raise BranchError(f"A remote folder blocks file {remote_path!r}.")
            # Size alone cannot distinguish an old equal-size file from the requested replacement.
            reconciled, replaced = self.upload_file(
                file, parent_uuid, remote_path, replacing=existing is not None,
                reconcile_by_size=existing is None or existing.size != file.size,
            )
            self.stats.files_uploaded += 1
            self.stats.files_replaced += int(replaced)
            self.stats.files_reconciled += int(reconciled)
            self.stats.upload_bytes += file.size
            self.progress.completed_uploads += 1
            self.progress.completed_bytes += file.size
        except (AuthenticationError, CancelledError):
            raise
        except (BranchError, OSError) as exc:
            self.stats.files_failed += 1
            self.fail(str(exc))

    def copy_directory(self, local: LocalDirectory, parent_uuid: str, remote_path: str) -> None:
        try:
            if self.cancel_event.is_set():
                raise CancelledError("Interrupted before copying the next directory.")
            if local.scan_error:
                raise BranchError(local.scan_error)
            for message in local.incomplete_errors:
                self.fail(message)
            unchanged, reason = local_directory_unchanged(local)
            if not unchanged:
                raise BranchError(reason)
            listing = self.list_remote(parent_uuid, remote_path)
            existing = listing.get(local.name)
            if existing is not None and existing.kind != "folder":
                raise BranchError(f"A remote file blocks directory {remote_path!r}.")
            folder_uuid = (existing.uuid if existing else
                           self.create_folder(parent_uuid, local.name, remote_path)[0])
            self.progress.folders_checked += 1
            for file in local.files:
                self.copy_file(file, folder_uuid, f"{remote_path}/{file.name}")
            for child in local.children:
                self.copy_directory(child, folder_uuid, f"{remote_path}/{child.name}")
            unchanged, reason = local_directory_unchanged(local)
            if not unchanged:
                raise BranchError(reason)
        except (AuthenticationError, CancelledError):
            raise
        except (BranchError, OSError) as exc:
            self.fail(str(exc))

    def process(self, source: LocalFile | LocalDirectory, parts: list[str]) -> int:
        self.progress.phase = "Resolving target"
        parent_uuid = self.resolve_target(parts)
        remote_path = "/" + "/".join(parts + [source.name])
        self.progress.phase = "Copying"
        self.progress.folders_checked = 0
        if isinstance(source, LocalDirectory):
            self.copy_directory(source, parent_uuid, remote_path)
        else:
            self.copy_file(source, parent_uuid, remote_path)
        # A late termination request still makes the run incomplete, even after the final operation.
        if self.cancel_event.is_set():
            raise CancelledError("Interrupted while completing the copy.")
        self.progress.clear()
        self.note(f"Files copied: {self.stats.files_uploaded} ({format_size(self.stats.upload_bytes)}); "
                  f"replacements: {self.stats.files_replaced}; folders created: {self.stats.folders_created}; "
                  f"CLI retries: {self.stats.retry_count}; reconciled uploads: {self.stats.files_reconciled}.")
        if self.stats.failures:
            self.note(f"Copy incomplete: {len(self.stats.failures)} unresolved item(s).")
            return 1
        self.note("Copy completed.")
        return 0


# Validate before remote work, then preserve the caller's login and restore signal handlers on exit.
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        parts = target_parts(args.target)
        source_path = Path(args.source).expanduser().absolute()
        source_stat = source_path.stat()
        if source_path.is_dir():
            if not args.recursive:
                raise BackupError("Source is a directory; use -r to copy it recursively.")
            if not source_path.name:
                raise BackupError("The filesystem root cannot be copied as a named directory.")
        elif not source_path.is_file():
            raise BackupError(f"Source is not a regular file or directory: {source_path}")
    except (BackupError, OSError) as exc:
        parser.error(str(exc))

    progress = Progress(time.monotonic(), total_folders=None)
    cancel_event = threading.Event()
    logger = configure_logging(args.verbose, False, name="internxtcp", prefix="internxtcp")
    old_handlers: dict[int, Any] = {}

    def request_cancel(signum: int, _frame: Any) -> None:
        cancel_event.set()
        if signum in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
            raise KeyboardInterrupt

    try:
        for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is not None:
                old_handlers[signum] = signal.signal(signum, request_cancel)
        progress.phase = "Scanning local files"
        source = (scan_local_tree(source_path, progress, logger, honor_ignore=False) if source_path.is_dir() else
                  LocalFile(source_path, source_path.name, source_path.name,
                            source_stat.st_size, source_stat.st_mtime_ns))
        command, version = resolve_cli()
        logger.info("Using Internxt CLI %s; command: %r", version, command)
        runner = CliRunner(command, logger, progress, cancel_event)
        runner.max_attempts = args.max_attempts
        copier = Copy(args=args, runner=runner, logger=logger, progress=progress,
                      cancel_event=cancel_event, allow_overwrite=True)
        progress.phase = "Checking login"
        session = copier.read_command(["whoami"], "whoami", "preflight")
        if session.get("success") is not True:
            raise AuthenticationError("Log in manually with `internxt login` and rerun.")
        del session
        return copier.process(source, parts)
    except (KeyboardInterrupt, CancelledError):
        cancel_event.set()
        progress.clear()
        logger.error("Copy interrupted.")
        return 130
    except (BackupError, OSError) as exc:
        progress.clear()
        logger.error("%s", exc)
        return 1
    finally:
        progress.clear()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        for handler in logger.handlers:
            handler.flush()


if __name__ == "__main__":
    raise SystemExit(main())
