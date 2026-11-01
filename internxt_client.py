#!/usr/bin/env python3

"""Shared CLI transport, retry/reconciliation, inventories and diagnostics for Internxt tools."""

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


def scan_local_tree(root: Path, progress: Progress, logger: logging.Logger, *,
                    honor_ignore: bool = True) -> LocalDirectory:
    """Inventory literal names; only backup mode prunes ignored subtrees."""
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
                    # Ignore markers select backup policy; a plain copy includes these directories.
                    if honor_ignore and marker.is_file():
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


# Both commands share mutation recovery so retry and diagnostic fixes apply to each.
class InternxtClient:
    """Own per-run CLI state without selecting backup or copy policy."""

    def __init__(self, *, args: argparse.Namespace, runner: CliRunner, logger: logging.Logger,
                 progress: Progress, cancel_event: threading.Event, allow_overwrite: bool = False):
        self.args = args
        self.runner = runner
        self.logger = logger
        self.progress = progress
        self.cancel_event = cancel_event
        self.allow_overwrite = allow_overwrite
        self.cache: dict[str, RemoteDirectory] = {}
        self.stats = BackupStats()
        self.started = progress.started

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
                if not self.allow_overwrite:
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
                    if not self.allow_overwrite:
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


# Each command gets a separate log with the same attempt/error reporting.
def configure_logging(verbose: bool, console_info: bool, *,
                      name: str = "internxt_backup", prefix: str = "backup") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    file_handler = logging.FileHandler(f"{prefix}_{timestamp}.log", mode="w", encoding="utf-8")
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

