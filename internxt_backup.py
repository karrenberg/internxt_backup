#!/usr/bin/env python3

"""Restartable, size-based synchronization through the official Internxt CLI."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import json
import logging
import math
import os
import platform
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable


IGNOREFILE_NAME = ".internxtignore"
MINIMUM_CLI_VERSION = (1, 6, 10)
FILE_SIZE_UPLOAD_LIMIT_BYTES = 100 * 1024**3
DEFAULT_COMMAND_TIMEOUT = 300.0
DEFAULT_UPLOAD_TIMEOUT = None
MAX_RECONCILIATION_READS = 5
MAX_ERROR_TEXT = 2000
TOKEN_WINDOW_MARGIN = 60.0
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class BackupError(RuntimeError):
    """An operational problem that prevents a branch or whole run from completing."""


class AuthenticationError(BackupError):
    """The current CLI session needs a manual login."""


class CancelledError(BackupError):
    """The user interrupted work and the owned CLI process was stopped."""


class BranchError(BackupError):
    """A remote branch could not be inspected or safely mutated."""


def format_size(byte_count: int) -> str:
    size = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_number(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number of seconds") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Keep argument construction side-effect-free so --help works without Node or a login."""
    parser = argparse.ArgumentParser(description="Back up or synchronize a local tree through the Internxt CLI.")
    parser.add_argument("-s", "--source", required=True, help="Source directory; a final * copies its contents directly")
    parser.add_argument("-t", "--target", required=True, help="Destination parent: root-relative folder path, UUID, or empty for Drive root")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log each CLI attempt to the log file")
    parser.add_argument("-l", "--full-console-log", action="store_true", help="Also print informational log messages to the console")
    parser.add_argument("-r", "--max_num_retries", "--max-attempts", dest="max_attempts", type=positive_int, default=5,
                        help="Maximum total CLI attempts per operation (default: 5)")
    parser.add_argument("-w", "--retry_wait_seconds", dest="retry_wait_seconds", type=positive_int, default=3,
                        help="Base retry delay in seconds; exponential waits are capped at 60 seconds (default: 3)")
    parser.add_argument("-d", "--allow-delete", "--allow_delete", dest="allow_delete", action="store_true",
                        help="Replace different-size files and delete remote-only or ignored items")
    parser.add_argument("--command-timeout", type=positive_number, default=DEFAULT_COMMAND_TIMEOUT,
                        help="Whole-command metadata deadline in seconds (default: 300)")
    parser.add_argument("--upload-timeout", type=positive_number, default=DEFAULT_UPLOAD_TIMEOUT,
                        help="Whole-command upload deadline; default is max(1800, 300 + size_bytes / 131072)")
    parser.add_argument("--upload-workers", type=int, choices=(1, 2), default=2,
                        help="Upload workers within this run (default: 2); falls back to 1 when token timing is uncertain")
    parser.add_argument("--no-prescan", action="store_true",
                        help="Plan and execute one folder at a time; starts earlier but still lists each existing remote folder")
    parser.epilog = (
        "Prescan is enabled by default: it plans the whole tree and shows exact upload totals. "
        "Execution reuses those listings, so healthy folders are not scanned twice. Both modes compare file names and sizes, not content hashes. "
        "Log in manually with `internxt login` before running; this script preserves the CLI session."
    )
    return parser


def validate_uuid(value: Any, description: str) -> str:
    if not isinstance(value, str) or not UUID_PATTERN.fullmatch(value):
        raise BranchError(f"{description} is missing or is not a UUID: {value!r}")
    try:
        uuid.UUID(value)
    except ValueError as exc:
        raise BranchError(f"{description} is not a valid UUID: {value!r}") from exc
    return value


def is_uuid(value: str) -> bool:
    return bool(UUID_PATTERN.fullmatch(value))


def check_cli_version(version_text: str) -> tuple[int, int, int]:
    match = re.search(r"@internxt/cli/(\d+)\.(\d+)\.(\d+)", version_text)
    if not match:
        raise BackupError(f"Could not determine Internxt CLI version from {version_text!r}.")
    version = tuple(map(int, match.groups()))
    if version < MINIMUM_CLI_VERSION:
        raise BackupError(
            f"Internxt CLI {version_text!r} is too old; install @internxt/cli >= 1.6.10 (UUIDv7 support is required)."
        )
    return version


def resolve_cli() -> tuple[list[str], str]:
    """Resolve the Windows Node entry point directly, avoiding shell expansion of filenames."""
    launcher = shutil.which("internxt") or shutil.which("internxt.cmd")
    if not launcher:
        raise BackupError("Internxt CLI was not found on PATH. Install it and log in manually first.")
    launcher_path = Path(launcher).resolve()
    if platform.system() == "Windows":
        command: list[str] | None = None
        for parent in (launcher_path.parent, *launcher_path.parents):
            entry = parent / "node_modules" / "@internxt" / "cli" / "bin" / "run.js"
            node = parent / "node.exe"
            if entry.is_file() and node.is_file():
                command = [str(node), str(entry)]
                break
        if command is None:
            raise BackupError(f"Could not resolve the Node executable and official CLI entry point beside {launcher_path}.")
    else:
        command = [str(launcher_path)]
    try:
        result = subprocess.run(command + ["--version"], check=True, capture_output=True, text=True,
                                encoding="utf-8", errors="strict", timeout=20, shell=False)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise BackupError(f"Could not run the Internxt CLI version check: {exc}") from exc
    version_text = result.stdout.strip() or result.stderr.strip()
    check_cli_version(version_text)
    return command, version_text


def resolve_npm_prefix(command: list[str]) -> str | None:
    """Record the global package prefix without making a network request."""
    if platform.system() == "Windows" and len(command) > 1:
        for parent in PureWindowsPath(command[1]).parents:
            if parent.name.casefold() == "node_modules":
                return str(parent.parent)
        return None
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        result = subprocess.run([npm, "prefix", "-g"], check=True, capture_output=True, text=True,
                                encoding="utf-8", errors="strict", timeout=10, shell=False)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def parse_cli_output(stdout: str) -> dict[str, Any] | None:
    """Accept a complete JSON envelope even when the CLI prefixes a diagnostic line."""
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("success"), bool):
            candidates.append(value)
    return candidates[-1] if candidates else None


def sanitize_error_text(value: str) -> str:
    """Keep actionable CLI diagnostics while stripping common credential forms before logging."""
    text = re.sub(r"<[^>]*>", " ", value)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+", r"\1<redacted>", text)
    text = re.sub(
        r"(?i)([\"']?(?:token|password|authorization|api[_-]?key|secret)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "<redacted-jwt>", text)
    return text[:MAX_ERROR_TEXT]


def error_message(data: dict[str, Any] | None, stdout: str, stderr: str, fallback: str) -> str:
    message = data.get("message") if isinstance(data, dict) else None
    if isinstance(message, str) and message.strip():
        return sanitize_error_text(message.strip())
    text = (stderr or stdout or fallback).strip()
    # CLI errors can be HTML or JSON fragments; retain only sanitized, bounded text.
    return sanitize_error_text(text or fallback)


def classify_error(message: str) -> str:
    text = message.casefold()
    if any(marker in text for marker in (
        "not logged in", "unauthorized", "unauthenticated", "authentication", "token expired", "invalid token", "401",
    )):
        return "auth"
    if any(marker in text for marker in ("file already exists", "folder already exists", "already exists")):
        return "conflict"
    if any(marker in text for marker in (
        "502", "503", "504", "bad gateway", "gateway timeout", "server unavailable", "temporarily unavailable",
        "econnreset", "econnrefused", "eai_again", "etimedout", "socket hang up", "timed out", "timeout",
        "network error", "connection reset", "connection refused",
    )):
        return "transient"
    if any(marker in text for marker in (
        "not valid", "invalid argument", "permission denied", "readonly database", "read-only database",
        "forbidden", "quota", "account limit", "storage limit",
        "not enough space", "file too large", "exceeds the maximum", "no such file", "cannot access",
    )):
        return "permanent"
    return "unknown"


@dataclass
class CommandResult:
    success: bool
    data: dict[str, Any] | None
    message: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool = False
    request_id: str | None = None
    stderr: str = ""


# Token claims only schedule already-authenticated CLI commands; they never replace the CLI's authentication checks.
def session_refresh_at(session: dict[str, Any]) -> float | None:
    """Mirror CLI 1.6.10's half-lifetime / missing-iat six-hour refresh window without retaining secrets."""
    login = session.get("login")
    token = login.get("token") if isinstance(login, dict) else None
    if not isinstance(token, str) or len(token.split(".")) != 3:
        return None
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(claims, dict):
        return None
    expires = claims.get("exp")
    issued = claims.get("iat")
    try:
        if type(expires) not in (int, float) or not math.isfinite(expires) or expires <= 0:
            return None
        if issued is None:
            return float(expires) - 6 * 60 * 60
        if type(issued) not in (int, float) or not math.isfinite(issued) or not 0 <= issued < expires:
            return None
        return float(issued) + (float(expires) - float(issued)) / 2
    except OverflowError:
        return None


# The coordinator probes refresh timing only after workers drain; each CLI still performs its normal authentication.
class ParallelUploadGate:
    """Cache a conservative refresh boundary; unavailable timing disables parallelism for this run."""

    def __init__(self, read_session: Callable[[], dict[str, Any]], logger: logging.Logger,
                 wall_clock: Callable[[], float] = time.time,
                 steady_clock: Callable[[], float] = time.monotonic):
        self.read_session = read_session
        self.logger = logger
        self.wall_clock = wall_clock
        self.steady_clock = steady_clock
        self.refresh_at: float | None = None
        self.steady_refresh_at: float | None = None
        self.disabled = False

    def observe(self, session: dict[str, Any]) -> None:
        self.refresh_at = session_refresh_at(session)
        self.steady_refresh_at = (self.steady_clock() + self.refresh_at - self.wall_clock()
                                  if self.refresh_at is not None else None)

    def remaining(self) -> float:
        if self.refresh_at is None or self.steady_refresh_at is None:
            return 0.0
        # A wall-clock correction must never enlarge the previously granted execution window.
        return min(self.refresh_at - self.wall_clock(), self.steady_refresh_at - self.steady_clock())

    def allows(self, timeout: float) -> bool:
        return not self.disabled and self.remaining() > timeout + TOKEN_WINDOW_MARGIN

    def prepare(self, timeout: float) -> bool:
        if self.disabled:
            return False
        if self.remaining() > TOKEN_WINDOW_MARGIN:
            return self.allows(timeout)
        try:
            for _ in range(2):
                self.observe(self.read_session())
                if self.remaining() > TOKEN_WINDOW_MARGIN:
                    return self.allows(timeout)
                # whoami can return the old token after refreshing, or silently hide refresh failure.
                # Read it again serially; only a fresh token actually returned from disk grants a window.
        except AuthenticationError:
            raise
        except BranchError:
            pass
        self.disabled = True
        self.logger.warning("Using sequential uploads: a safe CLI token refresh window could not be verified.")
        return False


class Progress:
    """Render one live status line; redirected logs get a flushed heartbeat every 15 seconds."""

    def __init__(self, started: float, total_folders: int | None, *, enabled: bool = True):
        self.started = started
        self.total_folders = total_folders
        self.enabled = enabled
        self.tty = sys.stdout.isatty()
        self.phase = "Starting"
        self.folders_checked = 0
        self.rename_folders = 0
        self.current_path = ""
        self.planned_uploads: int | None = None
        self.planned_bytes: int | None = None
        self.completed_uploads = 0
        self.completed_bytes = 0
        self.operation = ""
        self.operation_started = 0.0
        self.attempt = 0
        self.max_attempts = 0
        self.retry_countdown: float | None = None
        self.last_render = 0.0
        self.last_line_length = 0

    def note(self, message: str) -> None:
        self.clear()
        print(message, flush=True)

    def clear(self) -> None:
        if self.tty and self.last_line_length:
            sys.stdout.write("\r" + " " * self.last_line_length + "\r")
            sys.stdout.flush()
        self.last_line_length = 0

    def set_operation(self, name: str, path: str, attempt: int, max_attempts: int) -> None:
        self.operation = name
        self.operation_started = time.monotonic()
        self.attempt = attempt
        self.max_attempts = max_attempts
        self.retry_countdown = None
        self.tick()

    def set_retry_countdown(self, seconds: float) -> None:
        self.retry_countdown = time.monotonic() + seconds
        self.tick(force=True)

    def clear_operation(self) -> None:
        self.operation = ""
        self.operation_started = 0.0
        self.retry_countdown = None

    def tick(self, *, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        interval = 1.0 if self.tty else 15.0
        if not force and now - self.last_render < interval:
            return
        elapsed = format_duration(now - self.started)
        folder_status = (f"folders {self.folders_checked}/{self.total_folders}"
                         if self.total_folders is not None else f"folders scanned {self.folders_checked}")
        message = f"{self.phase}: {folder_status}, rename candidates {self.rename_folders}, elapsed {elapsed}"
        if self.planned_uploads is not None and self.planned_bytes is not None:
            message += (
                f", uploads {self.completed_uploads}/{self.planned_uploads}"
                f" ({format_size(self.completed_bytes)}/{format_size(self.planned_bytes)})"
            )
        if self.operation:
            age = format_duration(now - self.operation_started)
            message += f", waiting for {self.operation} {age} (attempt {self.attempt}/{self.max_attempts})"
        if self.retry_countdown is not None:
            message += f", retry in {max(0, self.retry_countdown - now):.0f}s"
        if self.current_path:
            message += f" | {self.current_path}"
        if self.tty:
            padded = message + " " * max(0, self.last_line_length - len(message))
            sys.stdout.write("\r" + padded)
            self.last_line_length = len(message)
        else:
            print(message, flush=True)
        self.last_render = now


class CliRunner:
    """Own CLI child processes and guarantee their process trees stop on timeout or cancellation."""

    def __init__(self, command: list[str], logger: logging.Logger, progress: Progress, cancel_event: threading.Event):
        self.command = command
        self.logger = logger
        self.progress = progress
        self.cancel_event = cancel_event
        self.max_attempts = 5

    def once(self, args: list[str], *, operation: str, path: str, attempt: int, timeout: float,
             report_progress: bool = True) -> CommandResult:
        command = self.command + args + ["--json", "-x"]
        started = time.monotonic()
        process: subprocess.Popen[str] | None = None
        self.logger.info("CLI attempt %s/%s: %s %s", attempt, self.max_attempts, operation, path)
        if self.cancel_event.is_set():
            raise CancelledError("Interrupted before starting the next Internxt CLI process.")
        if report_progress:
            self.progress.set_operation(operation, path, attempt, self.max_attempts)
        timed_out = False
        try:
            windows = platform.system() == "Windows"
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                shell=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
                start_new_session=not windows,
            )
            while True:
                if self.cancel_event.is_set():
                    self._terminate_tree(process)
                    stdout, stderr = process.communicate()
                    raise CancelledError("Interrupted; the owned Internxt CLI process was stopped.")
                try:
                    stdout, stderr = process.communicate(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    age = time.monotonic() - started
                    if age >= timeout:
                        timed_out = True
                        self._terminate_tree(process)
                        stdout, stderr = process.communicate()
                        break
                    if report_progress:
                        self.progress.tick()
            data = parse_cli_output(stdout or "")
            if timed_out:
                message = f"command timed out after {timeout:g} seconds"
                success = False
            else:
                message = error_message(data, stdout or "", stderr or "", f"CLI exited with status {process.returncode}")
                success = process.returncode == 0 and isinstance(data, dict) and data.get("success") is True
            request_id = data.get("requestId") if isinstance(data, dict) and isinstance(data.get("requestId"), str) else None
            result = CommandResult(success, data, "" if success else message, process.returncode,
                                   time.monotonic() - started, timed_out, request_id,
                                   sanitize_error_text((stderr or "")[-MAX_ERROR_TEXT:]))
        except CancelledError:
            raise
        except KeyboardInterrupt:
            if process is not None and process.poll() is None:
                self._terminate_tree(process)
                try:
                    process.communicate()
                except (OSError, UnicodeError, subprocess.SubprocessError):
                    pass
            raise
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            if process is not None and process.poll() is None:
                self._terminate_tree(process)
                try:
                    process.communicate()
                except (OSError, UnicodeError, subprocess.SubprocessError):
                    pass
            result = CommandResult(False, None, str(exc)[:MAX_ERROR_TEXT], process.returncode if process else None,
                                   time.monotonic() - started, False)
        finally:
            if report_progress:
                self.progress.clear_operation()
        # whoami includes private login material; even malformed/error output must never enter a diagnostic log.
        if operation == "whoami":
            result.stderr = ""
            if not result.success:
                kind = classify_error(result.message)
                result.message = {
                    "auth": "Internxt authentication failed; log in manually and rerun.",
                    "transient": "Internxt session check failed: server unavailable or command timed out.",
                }.get(kind, "Internxt session check returned an invalid response.")
        self.logger.info("CLI result: %s %s attempt=%d exit=%s duration=%.2fs request_id=%s success=%s error=%r stderr=%r",
                         operation, path, attempt, result.exit_code, result.duration_seconds,
                         result.request_id, result.success, result.message[:MAX_ERROR_TEXT], result.stderr[-MAX_ERROR_TEXT:])
        return result

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if platform.system() == "Windows":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                               text=True, timeout=10, check=False, shell=False)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if process.poll() is None:
            process.kill()


@dataclass
class LocalFile:
    path: Path
    name: str
    relative_path: str
    size: int
    mtime_ns: int


@dataclass
class LocalDirectory:
    path: Path
    name: str
    relative_path: str
    parent_path: str
    direct_names: tuple[str, ...]
    mtime_ns: int
    identity: tuple[int, int] = (0, 0)
    children: list["LocalDirectory"] = field(default_factory=list)
    files: list[LocalFile] = field(default_factory=list)
    ignored_children: list[tuple[str, tuple[int, int] | None]] = field(default_factory=list)
    incomplete_errors: list[str] = field(default_factory=list)
    scan_error: str | None = None


def scan_local_tree(root: Path, progress: Progress, logger: logging.Logger) -> LocalDirectory:
    """Build one literal-name local inventory, pruning ignored subtrees and retaining scan failures."""
    try:
        root_stat = root.stat()
    except OSError as exc:
        raise BackupError(f"Cannot access source directory {root}: {exc}") from exc
    if not root.is_dir():
        raise BackupError(f"Source is not a directory: {root}")

    root_entry = LocalDirectory(root, root.name, ".", "", (), root_stat.st_mtime_ns,
                                (root_stat.st_dev, root_stat.st_ino))
    stack = [root_entry]
    while stack:
        current = stack.pop()
        progress.folders_checked += 1
        progress.current_path = current.relative_path
        try:
            entries = []
            with os.scandir(current.path) as iterator:
                for entry in iterator:
                    entries.append(entry)
                    if len(entries) % 1000 == 0:
                        progress.current_path = current.relative_path
                        progress.tick()
            entries.sort(key=lambda entry: entry.name)
        except OSError as exc:
            current.scan_error = f"Could not scan {current.path}: {exc}"
            logger.error(current.scan_error)
            progress.tick()
            continue
        current.direct_names = tuple(entry.name for entry in entries)
        dirs_to_visit: list[LocalDirectory] = []
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    child_path = Path(entry.path)
                    marker = child_path / IGNOREFILE_NAME
                    if marker.is_file():
                        try:
                            marker_stat = marker.stat()
                            marker_snapshot = (marker_stat.st_size, marker_stat.st_mtime_ns)
                        except OSError:
                            marker_snapshot = None
                        current.ignored_children.append((entry.name, marker_snapshot))
                        if marker_snapshot is None:
                            current.incomplete_errors.append(f"Could not read ignore marker: {marker}")
                        continue
                    child_stat = child_path.stat()
                    rel = entry.name if current.relative_path == "." else f"{current.relative_path}/{entry.name}"
                    child = LocalDirectory(child_path, entry.name, rel, current.relative_path, (), child_stat.st_mtime_ns,
                                           (child_stat.st_dev, child_stat.st_ino))
                    current.children.append(child)
                    dirs_to_visit.append(child)
                elif entry.is_file(follow_symlinks=True):
                    file_stat = entry.stat(follow_symlinks=True)
                    rel = entry.name if current.relative_path == "." else f"{current.relative_path}/{entry.name}"
                    current.files.append(LocalFile(Path(entry.path), entry.name, rel, file_stat.st_size, file_stat.st_mtime_ns))
                else:
                    error = f"Unsupported filesystem entry: {entry.path}"
                    current.incomplete_errors.append(error)
                    logger.error(error)
            except OSError as exc:
                error = f"Could not inspect {entry.path}: {exc}"
                current.incomplete_errors.append(error)
                logger.error(error)
        stack.extend(reversed(dirs_to_visit))
        progress.current_path = current.relative_path
        progress.tick()
    return root_entry


def local_directory_unchanged(directory: LocalDirectory) -> tuple[bool, str]:
    try:
        current_stat = directory.path.stat()
        with os.scandir(directory.path) as iterator:
            current_names = tuple(sorted(entry.name for entry in iterator))
        expected_names = tuple(sorted(directory.direct_names))
        # Directory mtime is noisy on some Windows filesystems; direct names and stable identity catch
        # actionable additions, removals, replacements, while each known file is checked separately.
        if (current_stat.st_dev, current_stat.st_ino) != directory.identity or current_names != expected_names:
            return False, (
                f"Directory changed during backup: {directory.path} "
                f"(identity {directory.identity!r}->{(current_stat.st_dev, current_stat.st_ino)!r}, "
                f"entries {expected_names!r}->{current_names!r})"
            )
        for file in directory.files:
            current = file.path.stat()
            if current.st_size != file.size or current.st_mtime_ns != file.mtime_ns:
                return False, f"Source file changed during backup: {file.path}"
        for name, snapshot in directory.ignored_children:
            marker = directory.path / name / IGNOREFILE_NAME
            try:
                current = marker.stat()
            except OSError:
                return False, f"Ignore marker changed during backup: {marker}"
            if snapshot is None or (current.st_size, current.st_mtime_ns) != snapshot:
                return False, f"Ignore marker changed during backup: {marker}"
        return True, ""
    except OSError as exc:
        return False, f"Could not recheck source directory {directory.path}: {exc}"


def file_unchanged(file: LocalFile) -> bool:
    try:
        current = file.path.stat()
    except OSError:
        return False
    return current.st_size == file.size and current.st_mtime_ns == file.mtime_ns


@dataclass
class RemoteItem:
    kind: str
    uuid: str
    name: str
    size: int | None
    raw: dict[str, Any]


@dataclass
class RemoteDirectory:
    files: dict[str, RemoteItem] = field(default_factory=dict)
    folders: dict[str, RemoteItem] = field(default_factory=dict)

    def get(self, name: str) -> RemoteItem | None:
        return self.files.get(name) or self.folders.get(name)

    def all_items(self) -> list[RemoteItem]:
        return list(self.files.values()) + list(self.folders.values())

    def put(self, item: RemoteItem) -> None:
        collection = self.files if item.kind == "file" else self.folders
        other = self.folders if item.kind == "file" else self.files
        if item.name in other:
            raise BranchError(f"Remote file/directory collision appeared for {item.name!r}.")
        old = collection.get(item.name)
        if old and old.uuid != item.uuid:
            collection.pop(item.name, None)
        collection[item.name] = item

    def remove_uuid(self, target_uuid: str) -> None:
        for collection in (self.files, self.folders):
            for name, item in list(collection.items()):
                if item.uuid == target_uuid:
                    collection.pop(name)


def parse_file_size(value: Any) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value):
        return int(value)
    raise BranchError(f"Remote file size is not a nonnegative byte count: {value!r}")


def remote_item_name(raw: dict[str, Any], kind: str) -> str:
    plain_name = raw.get("plainName")
    if not isinstance(plain_name, str) or not plain_name:
        raise BranchError(f"Remote {kind} is missing its literal plainName: uuid={raw.get('uuid')!r}")
    if kind == "folder":
        return plain_name
    extension = raw.get("type")
    if extension is None:
        extension = ""
    if not isinstance(extension, str):
        raise BranchError(f"Remote file has invalid type/extension {extension!r}.")
    return f"{plain_name}.{extension}" if extension else plain_name


def parse_remote_item(raw: Any, kind: str) -> RemoteItem:
    if not isinstance(raw, dict):
        raise BranchError(f"Remote {kind} entry is not an object.")
    item_uuid = validate_uuid(raw.get("uuid"), f"Remote {kind} UUID")
    name = remote_item_name(raw, kind)
    size = parse_file_size(raw.get("size")) if kind == "file" else None
    return RemoteItem(kind, item_uuid, name, size, raw)


def validate_listing(data: dict[str, Any] | None) -> RemoteDirectory:
    if not isinstance(data, dict) or data.get("success") is not True:
        raise BranchError("CLI list response is not a successful JSON envelope.")
    listing = data.get("list")
    if not isinstance(listing, dict):
        raise BranchError("CLI list response is missing its list object.")
    files_raw, folders_raw = listing.get("files"), listing.get("folders")
    if not isinstance(files_raw, list) or not isinstance(folders_raw, list):
        raise BranchError("CLI list response must contain files and folders arrays.")
    result = RemoteDirectory()
    identities: set[str] = set()
    for kind, entries in (("file", files_raw), ("folder", folders_raw)):
        collection = result.files if kind == "file" else result.folders
        for raw in entries:
            item = parse_remote_item(raw, kind)
            if item.uuid in identities:
                raise BranchError(f"CLI list response contains duplicate UUID {item.uuid}.")
            if item.name in collection:
                raise BranchError(f"CLI list response contains ambiguous {kind} name {item.name!r}.")
            identities.add(item.uuid)
            collection[item.name] = item
    collisions = result.files.keys() & result.folders.keys()
    if collisions:
        raise BranchError(f"Remote file/directory name collision(s): {sorted(collisions)!r}")
    return result


@dataclass
class BackupStats:
    folders_created: int = 0
    folders_renamed: int = 0
    folders_deleted: int = 0
    files_uploaded: int = 0
    files_replaced: int = 0
    files_reconciled: int = 0
    other_reconciled_outcomes: int = 0
    files_skipped_same_size: int = 0
    files_skipped_mismatch: int = 0
    files_too_large: int = 0
    files_failed: int = 0
    files_deleted: int = 0
    files_deleted_bytes: int = 0
    upload_bytes: int = 0
    retry_count: int = 0
    reconciliation_reads: int = 0
    listing_attempts: int = 0
    rename_candidates_inspected: int = 0
    failures: list[str] = field(default_factory=list)


@dataclass
class FilePlan:
    local: LocalFile
    action: str
    remote: RemoteItem | None = None


@dataclass
class DirectoryPlan:
    local: LocalDirectory
    remote_uuid: str | None
    parent_uuid: str | None
    remote_name: str | None
    rename_from: RemoteItem | None = None
    listing: RemoteDirectory | None = None
    files: list[FilePlan] = field(default_factory=list)
    children: list["DirectoryPlan"] = field(default_factory=list)
    deletions: list[RemoteItem] = field(default_factory=list)
    error: str | None = None
    complete: bool = True
    renamed: bool = False
    created: bool = False
    conflict_recovered: bool = False


class Backup:
    """Plan and execute one local directory tree while updating a per-run remote listing cache."""

    def __init__(self, *, args: argparse.Namespace, runner: CliRunner, logger: logging.Logger,
                 progress: Progress, cancel_event: threading.Event, source_root: Path,
                 source_name: str, copy_contents: bool, local_root: LocalDirectory):
        self.args = args
        self.runner = runner
        self.logger = logger
        self.progress = progress
        self.cancel_event = cancel_event
        self.source_root = source_root
        self.source_name = source_name
        self.copy_contents = copy_contents
        self.local_root = local_root
        self.cache: dict[str, RemoteDirectory] = {}
        self.stats = BackupStats()
        self.started = progress.started
        self.total_folders = self.count_directories(local_root)
        self.progress.total_folders = self.total_folders
        self.progress.planned_uploads = None if args.no_prescan else 0
        self.progress.planned_bytes = None if args.no_prescan else 0
        self._planned_actions: set[str] = set()
        self.upload_gate = ParallelUploadGate(
            lambda: self.read_command(["whoami"], "whoami", "checking upload token window"), logger,
        )

    @staticmethod
    def count_directories(root: LocalDirectory) -> int:
        return 1 + sum(Backup.count_directories(child) for child in root.children)

    def note(self, message: str) -> None:
        self.logger.info(message)
        self.progress.note(message)

    def retry_wait(self, operation: str, attempt: int, path: str) -> None:
        delay = min(60.0, float(self.args.retry_wait_seconds) * (2 ** max(0, attempt - 1)))
        delay = random.uniform(delay / 2.0, delay)
        self.stats.retry_count += 1
        self.progress.current_path = path
        self.progress.set_retry_countdown(delay)
        self.logger.warning("Retrying %s after attempt %d; wait %.1f seconds (%s)", operation, attempt, delay, path)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise CancelledError("Interrupted during retry wait.")
            self.progress.tick()
            self.cancel_event.wait(min(1.0, max(0.0, deadline - time.monotonic())))
        self.progress.retry_countdown = None

    def read_command(self, args: list[str], operation: str, path: str, *, reconciliation: bool = False) -> dict[str, Any]:
        last_message = ""
        attempts_used = 0
        for attempt in range(1, self.args.max_attempts + 1):
            attempts_used = attempt
            result = self.runner.once(args, operation=operation, path=path, attempt=attempt,
                                      timeout=self.args.command_timeout)
            if result.success:
                if result.data is None:
                    raise BranchError(f"{operation} returned no JSON response ({path}).")
                return result.data
            last_message = result.message
            kind = classify_error(last_message)
            if kind == "auth":
                raise AuthenticationError(f"Internxt authentication failed during {operation}; log in manually and rerun.")
            if kind != "transient" or attempt == self.args.max_attempts:
                break
            self.retry_wait(operation, attempt, path)
        raise BranchError(f"{operation} failed after {attempts_used} attempt(s) for {path}: {last_message}")

    def list_remote(self, folder_uuid: str, path: str, *, reconciliation: bool = False, force: bool = False) -> RemoteDirectory:
        if not force and folder_uuid in self.cache:
            return self.cache[folder_uuid]
        if reconciliation:
            self.stats.reconciliation_reads += 1
        self.stats.listing_attempts += 1
        data = self.read_command(["list", f"--id={folder_uuid}"], "list", path, reconciliation=reconciliation)
        listing = validate_listing(data)
        self.cache[folder_uuid] = listing
        return listing

    def fresh_listing(self, folder_uuid: str, path: str) -> RemoteDirectory:
        return self.list_remote(folder_uuid, path, reconciliation=True, force=True)

    def resolve_target_parent(self) -> str:
        target = self.args.target
        if target == "":
            return ""
        if is_uuid(target):
            return validate_uuid(target, "Target folder UUID")
        parts = re.split(r"[\\/]+", target.strip("/\\"))
        if not parts or any(not part or part in (".", "..") for part in parts):
            raise BackupError(f"Invalid target folder path: {target!r}")
        current = ""
        walked: list[str] = []
        for part in parts:
            listing = self.list_remote(current, "/".join(walked) or "Drive root")
            item = listing.get(part)
            if item is None or item.kind != "folder":
                raise BackupError(f"Target folder does not exist: /{'/'.join(walked + [part])}")
            current = item.uuid
            walked.append(part)
        return current

    def remote_matches_local(self, local: LocalDirectory, remote_uuid: str) -> bool:
        """Reject a rename candidate on its immediate entries before inspecting descendants."""
        self.stats.rename_candidates_inspected += 1
        self.progress.rename_folders = self.stats.rename_candidates_inspected
        self.progress.current_path = local.relative_path
        self.progress.tick()
        if local.scan_error or local.incomplete_errors or local.ignored_children:
            return False
        remote = self.list_remote(remote_uuid, local.relative_path or ".")
        local_files = {item.name: item.size for item in local.files}
        remote_files = {name: item.size for name, item in remote.files.items()}
        local_dirs = {child.name for child in local.children}
        remote_dirs = set(remote.folders)
        if local_files != remote_files or local_dirs != remote_dirs:
            return False
        for child in local.children:
            if not self.remote_matches_local(child, remote.folders[child.name].uuid):
                return False
        return True

    def find_renames(self, local_candidates: dict[str, LocalDirectory], remote_listing: RemoteDirectory,
                     local_reserved_names: set[str]) -> tuple[dict[str, RemoteItem], bool]:
        local_only = {name: directory for name, directory in local_candidates.items()
                      if name not in remote_listing.folders and name not in remote_listing.files}
        remote_only = {name: item for name, item in remote_listing.folders.items()
                       if name not in local_candidates and name not in local_reserved_names}
        if not local_only or not remote_only:
            return {}, False
        matches: dict[str, list[str]] = {name: [] for name in local_only}
        failed_inspection = False
        for local_name, local in local_only.items():
            for remote_name, remote in remote_only.items():
                try:
                    if self.remote_matches_local(local, remote.uuid):
                        matches[local_name].append(remote_name)
                except BranchError as exc:
                    failed_inspection = True
                    self.logger.error("Could not inspect rename candidate %s (%s): %s", remote_name, remote.uuid, exc)
        reverse: dict[str, list[str]] = {}
        for local_name, remote_names in matches.items():
            for remote_name in remote_names:
                reverse.setdefault(remote_name, []).append(local_name)
        found: dict[str, RemoteItem] = {}
        for local_name, remote_names in matches.items():
            if len(remote_names) == 1 and len(reverse.get(remote_names[0], [])) == 1:
                found[local_name] = remote_listing.folders[remote_names[0]]
        return found, failed_inspection

    def resolve_root(self, target_parent: str) -> DirectoryPlan:
        if self.copy_contents:
            return DirectoryPlan(self.local_root, target_parent, None, None)
        parent_listing = self.list_remote(target_parent, ".")
        existing = parent_listing.get(self.source_name)
        if existing is not None:
            if existing.kind != "folder":
                raise BranchError(f"A remote file blocks source folder {self.source_name!r}; resolve the collision manually.")
            return DirectoryPlan(self.local_root, existing.uuid, target_parent, self.source_name)
        rename_from = None
        rename_scan_failed = False
        if self.args.allow_delete:
            renamed, rename_scan_failed = self.find_renames(
                {self.source_name: self.local_root}, parent_listing, {self.source_name}
            )
            rename_from = renamed.get(self.source_name)
            if rename_scan_failed:
                raise BranchError("Could not safely inspect all remote rename candidates for the source root; no mutation was made.")
        plan = DirectoryPlan(self.local_root, rename_from.uuid if rename_from else None, target_parent,
                             self.source_name, rename_from=rename_from)
        return plan

    def plan_directory(self, plan: DirectoryPlan, *, recursive: bool) -> None:
        """Use the same per-folder action planner for upfront and incremental execution."""
        self.progress.phase = "Pre-scanning remote" if not self.args.no_prescan else "Checking folders"
        if not self.args.no_prescan:
            self.progress.folders_checked += 1
        self.progress.current_path = plan.local.relative_path
        self.progress.tick()
        if plan.error:
            plan.complete = False
            if plan.error not in self.stats.failures:
                self.stats.failures.append(plan.error)
            return
        if plan.local.scan_error:
            plan.complete = False
            plan.error = plan.local.scan_error
            self.stats.failures.append(plan.error)
            return
        if plan.local.incomplete_errors:
            plan.complete = False
            self.stats.failures.extend(plan.local.incomplete_errors)
        try:
            plan.listing = self.list_remote(plan.remote_uuid, plan.local.relative_path or ".") if plan.remote_uuid else RemoteDirectory()
        except (AuthenticationError, CancelledError):
            raise
        except BranchError as exc:
            plan.complete = False
            plan.error = str(exc)
            self.stats.failures.append(plan.error)
            self.stats.files_failed += len(plan.local.files)
            return

        listing = plan.listing
        local_file_names = {item.name for item in plan.local.files}
        local_dir_names = {item.name for item in plan.local.children}
        local_dir_map = {item.name: item for item in plan.local.children}
        rename_matches: dict[str, RemoteItem] = {}
        rename_scan_failed = False
        if self.args.allow_delete and local_dir_names:
            rename_matches, rename_scan_failed = self.find_renames(local_dir_map, listing, local_file_names)
            if rename_scan_failed:
                plan.complete = False
                self.stats.failures.append(f"{plan.local.relative_path}: one or more rename candidates could not be inspected")

        used_uuids: set[str] = set()
        for file in plan.local.files:
            remote = listing.get(file.name)
            if remote is not None:
                used_uuids.add(remote.uuid)
            if file.size > FILE_SIZE_UPLOAD_LIMIT_BYTES:
                plan.files.append(FilePlan(file, "too_large", remote))
                plan.complete = False
            elif remote is not None and remote.kind == "folder":
                plan.files.append(FilePlan(file, "conflict", remote))
                plan.complete = False
            elif remote is not None and remote.size == file.size:
                plan.files.append(FilePlan(file, "skip_same", remote))
            elif remote is not None and not self.args.allow_delete:
                plan.files.append(FilePlan(file, "skip_mismatch", remote))
            elif remote is not None:
                plan.files.append(FilePlan(file, "replace", remote))
            else:
                plan.files.append(FilePlan(file, "upload"))

        for child in plan.local.children:
            remote = listing.get(child.name)
            if remote is not None:
                used_uuids.add(remote.uuid)
                if remote.kind == "file":
                    child_plan = DirectoryPlan(child, None, plan.remote_uuid, child.name,
                                               error=f"A remote file blocks local folder {child.relative_path!r}.", complete=False)
                    plan.complete = False
                    self.stats.failures.append(child_plan.error or "Folder/file collision")
                else:
                    child_plan = DirectoryPlan(child, remote.uuid, plan.remote_uuid, child.name)
            elif child.name in rename_matches:
                remote = rename_matches[child.name]
                used_uuids.add(remote.uuid)
                child_plan = DirectoryPlan(child, remote.uuid, plan.remote_uuid, child.name, rename_from=remote)
            else:
                child_plan = DirectoryPlan(child, None, plan.remote_uuid, child.name)
            plan.children.append(child_plan)

        if self.args.allow_delete and plan.complete and not rename_scan_failed:
            plan.deletions = [item for item in listing.all_items() if item.uuid not in used_uuids]

        if recursive:
            for child_plan in plan.children:
                if child_plan.error:
                    continue
                self.plan_directory(child_plan, recursive=True)
                if not child_plan.complete:
                    plan.complete = False
            if not plan.complete:
                plan.deletions.clear()

        if not self.args.no_prescan:
            for action in plan.files:
                if action.action in ("upload", "replace"):
                    self.progress.planned_uploads = (self.progress.planned_uploads or 0) + 1
                    self.progress.planned_bytes = (self.progress.planned_bytes or 0) + action.local.size

    def empty_created_folder(self, folder_uuid: str) -> None:
        self.cache[folder_uuid] = RemoteDirectory()

    def update_parent_folder(self, parent_uuid: str, item: RemoteItem) -> None:
        listing = self.cache.get(parent_uuid)
        if listing is not None:
            listing.remove_uuid(item.uuid)
            listing.put(item)

    def create_folder(self, parent_uuid: str, name: str, path: str) -> tuple[str, bool]:
        known_reads = 0
        for attempt in range(1, self.args.max_attempts + 1):
            result = self.runner.once(["create-folder", f"--id={parent_uuid}", "--name", name],
                                      operation="create-folder", path=path, attempt=attempt,
                                      timeout=self.args.command_timeout)
            if result.success and isinstance(result.data, dict):
                raw = result.data.get("folder")
                try:
                    item = parse_remote_item(raw, "folder")
                except BranchError:
                    item = None
                if item is not None and item.name == name:
                    self.update_parent_folder(parent_uuid, item)
                    self.empty_created_folder(item.uuid)
                    self.stats.folders_created += 1
                    return item.uuid, False
            reported_success = result.success
            kind = classify_error(result.message)
            if kind == "auth":
                raise AuthenticationError("Internxt authentication failed during create-folder; log in manually and rerun.")
            if kind == "permanent":
                raise BranchError(f"Could not create folder {path!r}: {result.message}")
            if known_reads >= MAX_RECONCILIATION_READS:
                raise BranchError(f"Unresolved create-folder result for {path!r}: {result.message}")
            reconciled = self.reconcile_named(parent_uuid, name, path, "folder", known_reads)
            known_reads += reconciled[1]
            found = reconciled[0]
            if found is not None:
                self.stats.other_reconciled_outcomes += 1
                return found.uuid, True
            if reported_success:
                while known_reads < MAX_RECONCILIATION_READS:
                    later, reads = self.reconcile_named(parent_uuid, name, path, "folder", known_reads)
                    known_reads += reads
                    if later is not None:
                        self.stats.other_reconciled_outcomes += 1
                        return later.uuid, True
                raise BranchError(f"create-folder reported success without exposing exact folder {path!r}; refusing another create.")
            if kind == "conflict":
                while known_reads < MAX_RECONCILIATION_READS:
                    later, reads = self.reconcile_named(parent_uuid, name, path, "folder", known_reads)
                    known_reads += reads
                    if later is not None:
                        self.stats.other_reconciled_outcomes += 1
                        return later.uuid, True
                raise BranchError(f"Create-folder reported already exists but no exact folder is visible for {path!r}.")
            if kind not in ("transient", "unknown") or attempt == self.args.max_attempts:
                break
            self.retry_wait("create-folder", attempt, path)
        raise BranchError(f"create-folder failed for {path!r}; the remote state could not be confirmed.")

    def reconcile_named(self, parent_uuid: str, name: str, path: str, kind: str,
                        already_read: int) -> tuple[RemoteItem | None, int]:
        read_count = 0
        observed_listing = False
        while already_read + read_count < MAX_RECONCILIATION_READS:
            try:
                listing = self.fresh_listing(parent_uuid, path)
            except AuthenticationError:
                raise
            except BranchError:
                read_count += 1
                continue
            read_count += 1
            observed_listing = True
            file_item, folder_item = listing.files.get(name), listing.folders.get(name)
            if file_item is not None and folder_item is not None:
                raise BranchError(f"Remote file/directory collision appeared while reconciling {path!r}.")
            if kind == "file" and folder_item is not None:
                raise BranchError(f"A remote folder blocks local file {path!r}; no replacement was attempted.")
            if kind == "folder" and file_item is not None:
                raise BranchError(f"A remote file blocks local folder {path!r}; no creation was attempted.")
            found = file_item if kind == "file" else folder_item
            if found is not None:
                return found, read_count
            return None, read_count
        if not observed_listing:
            raise BranchError(f"Could not inspect parent folder while reconciling {path!r}; the mutation outcome remains unknown.")
        return None, read_count

    def ensure_directory(self, plan: DirectoryPlan) -> bool:
        if plan.remote_uuid is None:
            if plan.parent_uuid is None or plan.remote_name is None:
                raise BranchError(f"New folder {plan.local.relative_path!r} has no parent identity.")
            uuid_value, recovered = self.create_folder(plan.parent_uuid, plan.remote_name, plan.local.relative_path)
            plan.remote_uuid = uuid_value
            plan.created = not recovered
            plan.conflict_recovered = recovered
            if recovered:
                # A conflicted folder may already contain data; it must be inspected before planning uploads.
                if not self.args.no_prescan:
                    old_count, old_bytes = self.count_planned_uploads(plan)
                    self.progress.planned_uploads = max(0, (self.progress.planned_uploads or 0) - old_count)
                    self.progress.planned_bytes = max(0, (self.progress.planned_bytes or 0) - old_bytes)
                plan.listing = None
                plan.files.clear()
                plan.children.clear()
                plan.deletions.clear()
                plan.error = None
                plan.complete = plan.local.scan_error is None
                self.plan_directory(plan, recursive=not self.args.no_prescan)
                return False
            return True
        if plan.rename_from is not None and not plan.renamed:
            if plan.parent_uuid is None or plan.remote_name is None:
                raise BranchError(f"Rename for {plan.local.relative_path!r} has no parent identity.")
            self.rename_folder(plan.parent_uuid, plan.rename_from, plan.remote_name, plan.local.relative_path)
            plan.renamed = True
        return False

    def rename_folder(self, parent_uuid: str, remote: RemoteItem, new_name: str, path: str) -> None:
        for attempt in range(1, self.args.max_attempts + 1):
            result = self.runner.once(["rename-folder", f"--id={remote.uuid}", "--name", new_name],
                                      operation="rename-folder", path=path, attempt=attempt,
                                      timeout=self.args.command_timeout)
            if result.success:
                listing = self.cache.get(parent_uuid)
                if listing is not None:
                    listing.remove_uuid(remote.uuid)
                    renamed = RemoteItem("folder", remote.uuid, new_name, None,
                                         {**remote.raw, "plainName": new_name})
                    listing.put(renamed)
                self.stats.folders_renamed += 1
                return
            kind = classify_error(result.message)
            if kind == "auth":
                raise AuthenticationError("Internxt authentication failed during rename-folder; log in manually and rerun.")
            if kind == "permanent":
                raise BranchError(f"Could not rename folder {path!r}: {result.message}")
            current = self.fresh_listing(parent_uuid, path)
            by_uuid = next((item for item in current.folders.values() if item.uuid == remote.uuid), None)
            if by_uuid and by_uuid.name == new_name:
                self.stats.folders_renamed += 1
                self.stats.other_reconciled_outcomes += 1
                return
            if by_uuid is None or by_uuid.name != remote.name:
                raise BranchError(f"Rename outcome for {path!r} is unresolved; original UUID {remote.uuid} was not found at its old or new name.")
            if attempt == self.args.max_attempts:
                break
            self.retry_wait("rename-folder", attempt, path)
        raise BranchError(f"rename-folder failed for {path!r}; original folder was preserved.")

    def record_mutation_file(self, parent_uuid: str, file_name: str, item: RemoteItem) -> None:
        if item.kind != "file" or item.name != file_name:
            raise BranchError(f"Upload response identity differs from requested file {file_name!r}.")
        listing = self.cache.setdefault(parent_uuid, RemoteDirectory())
        listing.remove_uuid(item.uuid)
        listing.put(item)

    def upload_timeout(self, file: LocalFile) -> float:
        return self.args.upload_timeout or max(1800.0, 300.0 + file.size / 131072.0)

    def upload_file(self, file: LocalFile, parent_uuid: str, path: str, *, replacing: bool,
                    first_result: CommandResult | None = None) -> tuple[bool, bool]:
        if not file_unchanged(file):
            raise BranchError(f"Source file changed before upload: {file.path}")
        timeout = self.upload_timeout(file)
        conflict_seen = False
        reconcile_reads = 0
        overwrite = replacing
        for attempt in range(1, self.args.max_attempts + 1):
            command = ["upload-file", "--file", str(file.path), f"--destination={parent_uuid}"]
            if overwrite:
                command.append("--overwrite")
            result = (first_result if attempt == 1 and first_result is not None else
                      self.runner.once(command, operation="upload-file", path=path, attempt=attempt, timeout=timeout))
            reported_success = result.success
            if result.success and isinstance(result.data, dict):
                try:
                    item = parse_remote_item(result.data.get("file"), "file")
                except BranchError:
                    item = None
                if item is not None and item.name == file.name and item.size == file.size:
                    self.record_mutation_file(parent_uuid, file.name, item)
                    if not file_unchanged(file):
                        raise BranchError(f"Source file changed while it was uploading: {file.path}")
                    return False, replacing
            kind = classify_error(result.message)
            if kind == "auth":
                raise AuthenticationError("Internxt authentication failed during upload; log in manually and rerun.")
            if kind == "permanent":
                raise BranchError(f"Upload of {path!r} was rejected: {result.message}")
            if reconcile_reads >= MAX_RECONCILIATION_READS:
                raise BranchError(f"Upload outcome for {path!r} remains unresolved after bounded reconciliation.")
            current, reads = self.reconcile_named(parent_uuid, file.name, path, "file", reconcile_reads)
            reconcile_reads += reads
            if current is None and reported_success:
                while reconcile_reads < MAX_RECONCILIATION_READS:
                    later, reads = self.reconcile_named(parent_uuid, file.name, path, "file", reconcile_reads)
                    reconcile_reads += reads
                    if later is not None:
                        current = later
                        break
                if current is None:
                    self.diagnose_name_mismatch(file.name, self.cache.get(parent_uuid))
                    raise BranchError(
                        f"upload-file reported success but no exact remote identity became visible for {path!r}; refusing another upload."
                    )
            if current is not None:
                if current.size == file.size:
                    self.record_mutation_file(parent_uuid, file.name, current)
                    if not file_unchanged(file):
                        raise BranchError(f"Source file changed during upload recovery: {file.path}")
                    return True, replacing
                if not self.args.allow_delete:
                    raise BranchError(f"A mismatched remote file appeared for {path!r}; replacement requires --allow-delete.")
                overwrite = True
            if kind == "conflict":
                conflict_seen = True
                if current is None:
                    while reconcile_reads < MAX_RECONCILIATION_READS:
                        later, reads = self.reconcile_named(parent_uuid, file.name, path, "file", reconcile_reads)
                        reconcile_reads += reads
                        if later is not None:
                            current = later
                            break
                    if current is None:
                        self.diagnose_name_mismatch(file.name, self.cache.get(parent_uuid))
                        break
                    if current.size == file.size:
                        self.record_mutation_file(parent_uuid, file.name, current)
                        if not file_unchanged(file):
                            raise BranchError(f"Source file changed during upload recovery: {file.path}")
                        self.stats.other_reconciled_outcomes += 1
                        return True, replacing
                    if not self.args.allow_delete:
                        break
                    overwrite = True
            if kind not in ("transient", "unknown", "conflict") or attempt == self.args.max_attempts:
                break
            if current is None and not conflict_seen:
                self.retry_wait("upload-file", attempt, path)
            elif current is not None:
                self.retry_wait("upload-file --overwrite", attempt, path)
        if conflict_seen:
            raise BranchError(
                f"CLI reported a file conflict for {path!r}, but no exact name/type/size match became visible; refusing another blind upload."
            )
        raise BranchError(f"Upload failed for {path!r}; remote state could not be confirmed.")

    def diagnose_name_mismatch(self, local_name: str, listing: RemoteDirectory | None) -> None:
        """Log plausible legacy name encodings for diagnosis without merging identities."""
        if listing is None:
            return
        from urllib.parse import unquote

        candidates = {unquote(local_name)}
        for codec in ("latin1", "cp1252"):
            try:
                candidates.add(local_name.encode(codec).decode("utf-8"))
            except (UnicodeError, LookupError):
                pass
        self.logger.error("Upload conflict name diagnostic (no identity was merged): local=%r; candidate transformations=%r; codepoints=%s",
                          local_name, sorted(candidates), " ".join(f"U+{ord(ch):04X}" for ch in local_name))
        for item in listing.all_items()[:10]:
            possible = item.name in candidates or unquote(item.name) == local_name
            codepoints = " ".join(f"U+{ord(character):04X}" for character in item.name)
            self.logger.error("Remote candidate%s: name=%r codepoints=%s uuid=%s size=%s raw=%r",
                              " (possible match only)" if possible else "", item.name, codepoints,
                              item.uuid, item.size, item.raw)

    def delete_remote(self, parent_uuid: str, item: RemoteItem, path: str) -> bool:
        command_name = "delete-permanently-file" if item.kind == "file" else "delete-permanently-folder"
        for attempt in range(1, self.args.max_attempts + 1):
            result = self.runner.once([command_name, f"--id={item.uuid}"], operation=command_name,
                                      path=path, attempt=attempt, timeout=self.args.command_timeout)
            if result.success:
                listing = self.cache.get(parent_uuid)
                if listing is not None:
                    listing.remove_uuid(item.uuid)
                if item.kind == "file":
                    self.stats.files_deleted += 1
                    self.stats.files_deleted_bytes += item.size or 0
                else:
                    self.stats.folders_deleted += 1
                return False
            kind = classify_error(result.message)
            if kind == "auth":
                raise AuthenticationError(f"Internxt authentication failed during {command_name}; log in manually and rerun.")
            if kind == "permanent":
                raise BranchError(f"Could not delete {path!r}: {result.message}")
            current = self.fresh_listing(parent_uuid, path)
            found = next((entry for entry in current.all_items() if entry.uuid == item.uuid), None)
            if found is None:
                if any(entry.name == item.name for entry in current.all_items()):
                    raise BranchError(f"Delete reconciliation found a different UUID with the same name at {path!r}; it was not deleted.")
                if item.kind == "file":
                    self.stats.files_deleted += 1
                    self.stats.files_deleted_bytes += item.size or 0
                else:
                    self.stats.folders_deleted += 1
                self.stats.other_reconciled_outcomes += 1
                return True
            if found.name != item.name or found.kind != item.kind:
                raise BranchError(f"Delete reconciliation found UUID {item.uuid} changed identity at {path!r}; stopping.")
            if attempt < self.args.max_attempts:
                self.retry_wait(command_name, attempt, path)
        raise BranchError(f"Delete failed for {path!r}; UUID {item.uuid} remains remote.")

    def execute_file(self, action: FilePlan, parent_uuid: str, *, first_result: CommandResult | None = None) -> bool:
        file = action.local
        self.progress.current_path = file.relative_path
        if action.action == "too_large":
            self.stats.files_too_large += 1
            message = f"File exceeds the Internxt 100 GiB limit and is incomplete: {file.relative_path} ({format_size(file.size)})"
            self.logger.error(message)
            self.stats.failures.append(message)
            return False
        if action.action == "conflict":
            self.stats.files_failed += 1
            message = f"Remote folder blocks local file {file.relative_path!r}; no deletion or upload was attempted."
            self.logger.error(message)
            self.stats.failures.append(message)
            return False
        if not file_unchanged(file):
            self.stats.files_failed += 1
            message = f"Source file changed after scanning: {file.path}"
            self.logger.error(message)
            self.stats.failures.append(message)
            return False
        if action.action == "skip_same":
            self.stats.files_skipped_same_size += 1
            return True
        if action.action == "skip_mismatch":
            self.stats.files_skipped_mismatch += 1
            self.logger.warning("Keeping different-size remote file %s (local %s, remote %s); use --allow-delete to replace.",
                                file.relative_path, format_size(file.size), format_size(action.remote.size if action.remote else 0))
            return True
        try:
            reconciled, replaced = self.upload_file(file, parent_uuid, file.relative_path,
                                                    replacing=action.action == "replace", first_result=first_result)
            self.stats.files_uploaded += 1
            if replaced:
                self.stats.files_replaced += 1
            if reconciled:
                self.stats.files_reconciled += 1
            self.stats.upload_bytes += file.size
            self.progress.completed_uploads += 1
            self.progress.completed_bytes += file.size
            return True
        except (AuthenticationError, CancelledError):
            raise
        except BranchError as exc:
            self.stats.files_failed += 1
            self.stats.failures.append(str(exc))
            self.logger.error("%s", exc)
            return False

    # Workers run only independent first upload attempts. All cache updates, retries and reconciliation stay serial.
    def run_upload_batch(self, actions: list[FilePlan], parent_uuid: str) -> list[CommandResult | None]:
        assert len(actions) == 2 and all(action.action in ("upload", "replace") for action in actions)
        def upload_once(action: FilePlan) -> CommandResult | None:
            file = action.local
            timeout = self.upload_timeout(file)
            if not self.upload_gate.allows(timeout) or not file_unchanged(file):
                return None
            command = ["upload-file", "--file", str(file.path), f"--destination={parent_uuid}"]
            if action.action == "replace":
                command.append("--overwrite")
            return self.runner.once(command, operation="upload-file", path=file.relative_path,
                                    attempt=1, timeout=timeout, report_progress=False)

        self.logger.info("Starting token-guarded batch of %d uploads (initial attempts only).", len(actions))
        self.progress.set_operation("upload-file (2 active)", "", 1, self.args.max_attempts)
        results: list[CommandResult | None] = [None] * len(actions)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            try:
                futures = {workers.submit(upload_once, action): index for index, action in enumerate(actions)}
                pending = set(futures)
                while pending:
                    if self.cancel_event.is_set():
                        raise CancelledError("Interrupted during parallel uploads.")
                    done, pending = concurrent.futures.wait(pending, timeout=1.0,
                                                           return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        result = future.result()
                        results[futures[future]] = result
                        if result is not None and not result.success and classify_error(result.message) == "auth":
                            raise AuthenticationError("Internxt authentication failed during upload; log in manually and rerun.")
                    self.progress.operation = f"upload-file ({len(pending)} active)"
                    self.progress.current_path = "; ".join(actions[futures[f]].local.relative_path for f in pending)
                    self.progress.tick()
            except BaseException:
                # Wake and reap every owned CLI before allowing interruption/auth failure to escape the executor.
                self.cancel_event.set()
                raise
            finally:
                self.progress.clear_operation()
        return results

    def execute_files(self, actions: list[FilePlan], parent_uuid: str) -> bool:
        """Pair independent uploads across local policy skips; consume every result on the coordinator thread."""
        complete = True
        uploads = []
        for action in actions:
            if self.cancel_event.is_set():
                raise CancelledError("Interrupted while processing the next file.")
            if action.action in ("upload", "replace"):
                uploads.append(action)
            elif not self.execute_file(action, parent_uuid):
                complete = False
            self.progress.tick()
        width = self.args.upload_workers
        for offset in range(0, len(uploads), width):
            if self.cancel_event.is_set():
                raise CancelledError("Interrupted before processing the next upload.")
            batch = uploads[offset:offset + width]
            results: list[CommandResult | None] = [None] * len(batch)
            if (len(batch) == 2 and all(file_unchanged(action.local) for action in batch)
                    and self.upload_gate.prepare(max(self.upload_timeout(action.local) for action in batch))):
                results = self.run_upload_batch(batch, parent_uuid)
            for action, result in zip(batch, results):
                if not self.execute_file(action, parent_uuid, first_result=result):
                    complete = False
        return complete

    def execute_directory(self, plan: DirectoryPlan, *, incremental: bool) -> bool:
        if self.cancel_event.is_set():
            raise CancelledError("Interrupted before processing the next folder.")
        if plan.error:
            plan.complete = False
            if plan.error not in self.stats.failures:
                self.stats.failures.append(plan.error)
            self.logger.error("Skipping incomplete folder %s: %s", plan.local.relative_path, plan.error)
            return False
        self.progress.phase = "Backing up"
        self.progress.folders_checked += 1
        self.progress.current_path = plan.local.relative_path
        self.progress.tick()
        unchanged, reason = local_directory_unchanged(plan.local)
        if not unchanged:
            plan.complete = False
            self.stats.failures.append(reason)
            self.logger.error("%s", reason)
            return False
        try:
            newly_created = self.ensure_directory(plan)
        except (AuthenticationError, CancelledError):
            raise
        except BranchError as exc:
            plan.complete = False
            self.stats.failures.append(str(exc))
            self.logger.error("Folder operation failed at %s: %s", plan.local.relative_path, exc)
            return False
        if newly_created:
            plan.listing = self.cache.get(plan.remote_uuid or "", RemoteDirectory())

        if plan.listing is None:
            plan.listing = self.list_remote(plan.remote_uuid or "", plan.local.relative_path or ".")

        complete = plan.complete
        if not self.execute_files(plan.files, plan.remote_uuid or ""):
            complete = False

        for child_plan in plan.children:
            if child_plan.error:
                complete = False
                continue
            if incremental:
                # Incremental mode plans this subtree only when its parent is ready to process it.
                self.plan_directory(child_plan, recursive=False)
            child_plan.parent_uuid = plan.remote_uuid
            if not self.execute_directory(child_plan, incremental=incremental):
                complete = False

        unchanged, reason = local_directory_unchanged(plan.local)
        if not unchanged:
            complete = False
            self.stats.failures.append(reason)
            self.logger.error("%s", reason)
        plan.complete = complete
        return complete

    def verify_source_plan(self, plan: DirectoryPlan) -> bool:
        """Recheck each directory and its direct files once before any planned cleanup starts."""
        if plan.local.scan_error:
            plan.complete = False
        else:
            unchanged, reason = local_directory_unchanged(plan.local)
            if not unchanged:
                plan.complete = False
                if reason not in self.stats.failures:
                    self.stats.failures.append(reason)
                self.logger.error("%s", reason)
        for child_plan in plan.children:
            if child_plan.error:
                child_plan.complete = False
            elif not self.verify_source_plan(child_plan):
                plan.complete = False
        return plan.complete

    def cleanup_directory(self, plan: DirectoryPlan) -> bool:
        """Delete bottom-up only after all planned uploads and a final source verification."""
        complete = plan.complete
        for child_plan in plan.children:
            if not self.cleanup_directory(child_plan):
                complete = False
        if not complete:
            plan.complete = False
            return False
        if not self.args.allow_delete or plan.listing is None:
            return True
        unchanged, reason = local_directory_unchanged(plan.local)
        if not unchanged:
            plan.complete = False
            if reason not in self.stats.failures:
                self.stats.failures.append(reason)
            self.logger.error("%s", reason)
            return False
        for item in plan.deletions:
            try:
                reconciled = self.delete_remote(
                    plan.remote_uuid or "", item,
                    f"{plan.local.relative_path}/{item.name}".strip("/"),
                )
                if reconciled:
                    self.logger.info("Deletion reconciled by UUID disappearance: %s", item.name)
            except (AuthenticationError, CancelledError):
                raise
            except BranchError as exc:
                complete = False
                self.stats.failures.append(str(exc))
                self.logger.error("%s", exc)
        plan.complete = complete
        return complete

    def count_planned_uploads(self, plan: DirectoryPlan) -> tuple[int, int]:
        count = sum(1 for action in plan.files if action.action in ("upload", "replace"))
        size = sum(action.local.size for action in plan.files if action.action in ("upload", "replace"))
        for child in plan.children:
            child_count, child_size = self.count_planned_uploads(child)
            count += child_count
            size += child_size
        return count, size

    def process(self) -> int:
        if self.local_root.scan_error:
            self.stats.failures.append(self.local_root.scan_error)
            self.logger.error("%s", self.local_root.scan_error)
            return self.finish()
        self.progress.phase = "Resolving target"
        target_parent = self.resolve_target_parent()
        root_plan = self.resolve_root(target_parent)
        if self.args.no_prescan:
            self.note(f"Local scan: {self.total_folders} folder(s); prescan disabled, remote totals will be reported as work completes.")
            self.progress.folders_checked = 0
            if root_plan.error:
                self.stats.failures.append(root_plan.error)
                self.logger.error("%s", root_plan.error)
                return self.finish()
            # Root identity is created/resolved before planning its contents, so this mode starts immediately.
            unchanged, reason = local_directory_unchanged(self.local_root)
            if not unchanged:
                self.stats.failures.append(reason)
                self.logger.error("%s", reason)
                return self.finish()
            try:
                self.ensure_directory(root_plan)
            except (AuthenticationError, CancelledError):
                raise
            except BranchError as exc:
                self.stats.failures.append(str(exc))
                self.logger.error("Source folder setup failed: %s", exc)
                return self.finish()
            if root_plan.listing is None:
                self.plan_directory(root_plan, recursive=False)
            self.execute_directory(root_plan, incremental=True)
        else:
            self.note(f"Local scan: {self.total_folders} folder(s), {format_size(sum(f.size for f in self.walk_files(self.local_root) if f.size <= FILE_SIZE_UPLOAD_LIMIT_BYTES))} total.")
            self.note("Pre-scanning remote to build the reusable action plan...")
            self.progress.phase = "Pre-scanning remote"
            self.progress.folders_checked = 0
            self.plan_directory(root_plan, recursive=True)
            count, size = self.count_planned_uploads(root_plan)
            self.progress.planned_uploads = count
            self.progress.planned_bytes = size
            self.note(f"Pre-scan complete: {count} file(s) ({format_size(size)}) planned for upload or replacement.")
            self.progress.folders_checked = 0
            self.progress.completed_uploads = 0
            self.progress.completed_bytes = 0
            self.progress.phase = "Backing up"
            self.execute_directory(root_plan, incremental=False)
        self.progress.phase = "Verifying source changes"
        self.verify_source_plan(root_plan)
        self.progress.phase = "Cleaning remote-only items"
        self.cleanup_directory(root_plan)
        return self.finish()

    @staticmethod
    def walk_files(root: LocalDirectory) -> list[LocalFile]:
        return root.files + [item for child in root.children for item in Backup.walk_files(child)]

    def finish(self) -> int:
        self.progress.clear()
        elapsed = format_duration(time.monotonic() - self.started)
        self.note("Backup summary:")
        self.note(f"  Folders created: {self.stats.folders_created}; renamed: {self.stats.folders_renamed}; deleted: {self.stats.folders_deleted}")
        self.note(f"  Files uploaded: {self.stats.files_uploaded} ({format_size(self.stats.upload_bytes)}); replacements: {self.stats.files_replaced}; reconciled: {self.stats.files_reconciled}")
        self.note(f"  Files skipped (same size): {self.stats.files_skipped_same_size}; retained mismatches: {self.stats.files_skipped_mismatch}")
        self.note(f"  Files deleted: {self.stats.files_deleted} ({format_size(self.stats.files_deleted_bytes)}); too large: {self.stats.files_too_large}; failed: {self.stats.files_failed}")
        self.note(f"  CLI retries: {self.stats.retry_count}; reconciliation listings: {self.stats.reconciliation_reads}; elapsed: {elapsed}")
        self.note(f"  Remote listing operations: {self.stats.listing_attempts}; rename-candidate folders inspected: {self.stats.rename_candidates_inspected}")
        self.note(f"  Other reconciled mutations: {self.stats.other_reconciled_outcomes}")
        if self.stats.failures:
            self.note(f"  Incomplete branches or files: {len(self.stats.failures)}")
            self.logger.error("Backup incomplete: %d unresolved or incomplete item(s).", len(self.stats.failures))
            return 1
        self.note("Backup completed under the selected size-based policy.")
        return 0


def source_details(source_argument: str) -> tuple[Path, str, bool]:
    raw = source_argument.rstrip("/\\")
    copy_contents = os.path.basename(raw) == "*"
    source = Path(os.path.dirname(raw) if copy_contents else raw).expanduser()
    if not source.exists():
        raise BackupError(f"Source does not exist: {source}")
    if not source.is_dir():
        raise BackupError(f"Source is not a directory: {source}")
    if copy_contents:
        return source.resolve(), source.name, True
    return source.resolve(), source.name, False


def configure_logging(verbose: bool, console_info: bool) -> logging.Logger:
    logger = logging.getLogger("internxt_backup")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    file_handler = logging.FileHandler(f"backup_{timestamp}.log", mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(file_handler)
    if console_info:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(console)
    else:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.WARNING)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(console)
    return logger


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        source_root, source_name, copy_contents = source_details(args.source)
        if not copy_contents and not source_name:
            raise BackupError("The filesystem root cannot be used as a named source directory.")
        if args.target != "" and not is_uuid(args.target):
            target_parts = re.split(r"[\\/]+", args.target.strip("/\\"))
            if not target_parts or any(not part or part in (".", "..") for part in target_parts):
                raise BackupError(f"Invalid target folder path: {args.target!r}")
    except BackupError as exc:
        parser.error(str(exc))

    started = time.monotonic()
    progress = Progress(started, total_folders=None)
    cancel_event = threading.Event()
    old_handlers: dict[int, Any] = {}

    def request_cancel(signum: int, _frame: Any) -> None:
        cancel_event.set()
        if signum in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
            raise KeyboardInterrupt

    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        handled_signals.append(signal.SIGBREAK)
    for signum in handled_signals:
        try:
            old_handlers[signum] = signal.signal(signum, request_cancel)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGHUP") and platform.system() != "Windows":
        old_handlers[signal.SIGHUP] = signal.signal(signal.SIGHUP, request_cancel)

    logger = configure_logging(args.verbose, args.full_console_log)
    try:
        command, version_text = resolve_cli()
        cli_version = check_cli_version(version_text)
        logger.info("Using Internxt CLI %s", version_text)
        node_path = command[0] if platform.system() == "Windows" else (shutil.which("node") or "node")
        try:
            node_version = subprocess.run([node_path, "--version"], check=True, capture_output=True,
                                          text=True, encoding="utf-8", errors="strict", timeout=20, shell=False).stdout.strip()
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            raise BackupError(f"Could not determine the Node.js runtime version: {exc}") from exc
        npm_prefix = resolve_npm_prefix(command)
        logger.info("Resolved CLI command: %r; npm prefix: %s; Node.js: %s", command,
                    npm_prefix or "(unavailable)", node_version)
        progress.phase = "Scanning local files"
        local_root = scan_local_tree(source_root, progress, logger)
        progress.total_folders = Backup.count_directories(local_root)
        if local_root.scan_error:
            logger.error("%s", local_root.scan_error)
            return 1
        progress.folders_checked = 0
        progress.phase = "Checking login"
        progress.current_path = ""
        progress.tick(force=True)
        runner = CliRunner(command, logger, progress, cancel_event)
        runner.max_attempts = args.max_attempts
        backup = Backup(args=args, runner=runner, logger=logger, progress=progress, cancel_event=cancel_event,
                        source_root=source_root, source_name=source_name, copy_contents=copy_contents,
                        local_root=local_root)
        session = backup.read_command(["whoami"], "whoami", "preflight")
        if session.get("success") is not True:
            raise AuthenticationError("The Internxt CLI session is not active; log in manually with `internxt login` and rerun. The session was not changed.")
        backup.upload_gate.observe(session)
        if cli_version != (1, 6, 10):
            backup.upload_gate.disabled = True
            if args.upload_workers == 2:
                logger.warning("Using sequential uploads: token refresh timing is verified only for CLI 1.6.10.")
        del session
        return backup.process()
    except KeyboardInterrupt:
        progress.clear()
        logger.error("Interrupted by user; stopping the owned CLI process.")
        return 130
    except CancelledError as exc:
        progress.clear()
        logger.error("%s", exc)
        return 130
    except AuthenticationError as exc:
        progress.clear()
        logger.error("%s", exc)
        return 1
    except BackupError as exc:
        progress.clear()
        logger.error("%s", exc)
        return 1
    finally:
        progress.clear()
        for signum, handler in old_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
