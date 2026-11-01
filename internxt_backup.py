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


# Preserve public helper imports while sharing their implementation with internxtcp.
from internxt_client import (
    AuthenticationError, BackupError, BackupStats, BranchError, CancelledError,
    CliRunner, CommandResult, DEFAULT_COMMAND_TIMEOUT, DEFAULT_UPLOAD_TIMEOUT,
    FILE_SIZE_UPLOAD_LIMIT_BYTES, IGNOREFILE_NAME, InternxtClient, LocalDirectory,
    LocalFile, MAX_ERROR_TEXT, MAX_RECONCILIATION_READS, MINIMUM_CLI_VERSION,
    ParallelUploadGate, Progress, RemoteDirectory, RemoteItem, TOKEN_WINDOW_MARGIN,
    UUID_PATTERN, check_cli_version, classify_error, configure_logging, error_message,
    file_unchanged, format_duration, format_size, is_uuid, local_directory_unchanged,
    parse_cli_output, parse_file_size, parse_remote_item, positive_int, positive_number,
    remote_item_name, resolve_cli, resolve_npm_prefix, sanitize_error_text,
    scan_local_tree, session_refresh_at, validate_listing, validate_uuid,
)


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


# Token claims only schedule already-authenticated CLI commands; they never replace the CLI's authentication checks.
# The coordinator probes refresh timing only after workers drain; each CLI still performs its normal authentication.
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


# Backup planning stays separate from the shared transport and recovery.
class Backup(InternxtClient):
    """Plan and execute one local directory tree while updating a per-run remote listing cache."""

    def __init__(self, *, args: argparse.Namespace, runner: CliRunner, logger: logging.Logger,
                 progress: Progress, cancel_event: threading.Event, source_root: Path,
                 source_name: str, copy_contents: bool, local_root: LocalDirectory):
        # Set up shared recovery before adding the backup-specific inventory and plan.
        super().__init__(args=args, runner=runner, logger=logger, progress=progress,
                         cancel_event=cancel_event, allow_overwrite=args.allow_delete)
        self.source_root = source_root
        self.source_name = source_name
        self.copy_contents = copy_contents
        self.local_root = local_root
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
