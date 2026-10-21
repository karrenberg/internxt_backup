import contextlib
import base64
import io
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import internxt_backup as backup_module


def new_uuid():
    return str(uuid.uuid4())


def remote_folder(name, folder_uuid=None):
    identity = folder_uuid or new_uuid()
    return backup_module.RemoteItem("folder", identity, name, None,
                                    {"type": "folder", "uuid": identity, "plainName": name})


def remote_file(name, size, file_uuid=None):
    path = Path(name)
    suffix = path.suffix
    plain_name = name[:-len(suffix)] if suffix else name
    extension = suffix[1:] if suffix else None
    identity = file_uuid or new_uuid()
    raw = {"uuid": identity, "plainName": plain_name, "type": extension, "size": str(size)}
    return backup_module.RemoteItem("file", identity, name, size, raw)


class FakeRemote:
    """A small in-memory CLI model with scripted mutation outcomes for recovery tests."""

    def __init__(self):
        self.root_uuid = new_uuid()
        self.directories = {"": backup_module.RemoteDirectory(), self.root_uuid: backup_module.RemoteDirectory()}
        self.parents = {self.root_uuid: ""}
        self.calls = []
        self.next_upload = None
        self.next_create = None
        self.next_delete = None
        self.next_rename = None
        self.next_list = None

    def add_folder(self, parent_uuid, name):
        item = remote_folder(name)
        self.directories.setdefault(parent_uuid, backup_module.RemoteDirectory()).put(item)
        self.directories[item.uuid] = backup_module.RemoteDirectory()
        self.parents[item.uuid] = parent_uuid
        return item

    def add_file(self, parent_uuid, name, size):
        item = remote_file(name, size)
        self.directories.setdefault(parent_uuid, backup_module.RemoteDirectory()).put(item)
        return item

    @staticmethod
    def json_item(item):
        return dict(item.raw, uuid=item.uuid, plainName=item.raw.get("plainName", item.name))

    def result(self, data, success=True, message=""):
        return backup_module.CommandResult(success, data, message, 0 if success else 1, 0.001)

    def once(self, args, *, operation, path, attempt, timeout, report_progress=True):
        self.calls.append((list(args), operation, path, attempt, timeout))
        command = args[0]
        if command == "whoami":
            return self.result({"success": True})
        if command == "list":
            folder_uuid = args[1].split("=", 1)[1]
            listing = self.directories.get(folder_uuid)
            if listing is None:
                return self.result({"success": False, "message": "Folder UUID not found"}, False, "Folder UUID not found")
            result = self.result({
                "success": True,
                "list": {
                    "files": [self.json_item(item) for item in listing.files.values()],
                    "folders": [self.json_item(item) for item in listing.folders.values()],
                },
            })
            if self.next_list:
                behavior, self.next_list = self.next_list, None
                return behavior(self, folder_uuid, result)
            return result
        if command == "create-folder":
            parent_uuid = args[1].split("=", 1)[1]
            name = args[3]
            listing = self.directories[parent_uuid]
            existing = listing.get(name)
            if existing:
                return self.result({"success": False, "message": "Folder already exists"}, False, "Folder already exists")
            item = self.add_folder(parent_uuid, name)
            result = self.result({"success": True, "folder": self.json_item(item)})
            if self.next_create:
                behavior, self.next_create = self.next_create, None
                return behavior(self, parent_uuid, name, item, result)
            return result
        if command == "upload-file":
            file_path = Path(args[2])
            parent_uuid = args[3].split("=", 1)[1]
            name = file_path.name
            listing = self.directories[parent_uuid]
            existing = listing.files.get(name)
            if name in listing.folders:
                return self.result({"success": False, "message": "File already exists"}, False, "File already exists")
            overwrite = "--overwrite" in args
            if existing and not overwrite:
                return self.result({"success": False, "message": "File already exists"}, False, "File already exists")
            item = remote_file(name, file_path.stat().st_size)
            listing.put(item)
            result = self.result({"success": True, "file": self.json_item(item)})
            if self.next_upload:
                behavior, self.next_upload = self.next_upload, None
                return behavior(self, parent_uuid, file_path, item, result)
            return result
        if command == "rename-folder":
            folder_uuid = args[1].split("=", 1)[1]
            new_name = args[3]
            parent_uuid = self.parents[folder_uuid]
            listing = self.directories[parent_uuid]
            current = next(item for item in listing.folders.values() if item.uuid == folder_uuid)
            listing.remove_uuid(folder_uuid)
            renamed = backup_module.RemoteItem("folder", folder_uuid, new_name, None,
                                                 {**current.raw, "plainName": new_name})
            listing.put(renamed)
            result = self.result({"success": True, "folder": self.json_item(renamed)})
            if self.next_rename:
                behavior, self.next_rename = self.next_rename, None
                return behavior(self, parent_uuid, renamed, result)
            return result
        if command == "delete-permanently-file":
            item_uuid = args[1].split("=", 1)[1]
            parent_uuid = next(parent for parent, listing in self.directories.items()
                               if any(item.uuid == item_uuid for item in listing.files.values()))
            item = next(item for item in self.directories[parent_uuid].files.values() if item.uuid == item_uuid)
            self.directories[parent_uuid].remove_uuid(item_uuid)
            result = self.result({"success": True})
            if self.next_delete:
                behavior, self.next_delete = self.next_delete, None
                return behavior(self, parent_uuid, item, result)
            return result
        if command == "delete-permanently-folder":
            item_uuid = args[1].split("=", 1)[1]
            parent_uuid = self.parents[item_uuid]
            self.directories[parent_uuid].remove_uuid(item_uuid)
            self.directories.pop(item_uuid, None)
            self.parents.pop(item_uuid, None)
            return self.result({"success": True})
        raise AssertionError(f"Unexpected fake CLI command: {args}")


def make_args(remote, *, allow_delete=False, no_prescan=False, max_attempts=3, upload_workers=1):
    return SimpleNamespace(
        target=remote.root_uuid,
        allow_delete=allow_delete,
        no_prescan=no_prescan,
        max_attempts=max_attempts,
        retry_wait_seconds=1,
        command_timeout=2.0,
        upload_timeout=2.0,
        upload_workers=upload_workers,
    )


def make_backup(source, remote, *, allow_delete=False, no_prescan=False, max_attempts=3, upload_workers=1):
    source = Path(source)
    args = make_args(remote, allow_delete=allow_delete, no_prescan=no_prescan, max_attempts=max_attempts,
                     upload_workers=upload_workers)
    logger = logging.getLogger(f"backup-test-{uuid.uuid4()}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    progress = backup_module.Progress(time.monotonic(), total_folders=0, enabled=False)
    local_root = backup_module.scan_local_tree(source, progress, logger)
    runner = remote
    runner.max_attempts = args.max_attempts
    backup = backup_module.Backup(args=args, runner=runner, logger=logger, progress=progress,
                                  cancel_event=threading.Event(), source_root=source,
                                  source_name=source.name, copy_contents=False, local_root=local_root)
    output = io.StringIO()
    def immediate_retry(_self, *_args):
        backup.stats.retry_count += 1
    with contextlib.redirect_stdout(output):
        with mock.patch.object(backup_module.Backup, "retry_wait", immediate_retry):
            status = backup.process()
    return backup, status, output.getvalue()


def test_import_and_help_have_no_cli_or_logging_side_effects(capsys):
    with mock.patch.object(backup_module, "resolve_cli", side_effect=AssertionError("CLI must not be checked for help")):
        with pytest.raises(SystemExit) as exit_info:
            backup_module.main(["--help"])
    assert exit_info.value.code == 0
    assert "--no-prescan" in capsys.readouterr().out


def test_integration_helper_never_exposes_session_payload():
    import conftest as integration_helpers

    # Failed session probes must not leak sensitive payloads into pytest reports.
    response = backup_module.subprocess.CompletedProcess(
        ["whoami"], 1, json.dumps({"success": False, "message": "PRIVATE_MESSAGE",
                                  "login": {"mnemonic": "PRIVATE_WORDS", "privateKey": "PRIVATE_KEY"}}),
        "PRIVATE_STDERR")
    with mock.patch.object(integration_helpers, "_internxt_subprocess", return_value=response):
        with pytest.raises(RuntimeError, match="Session check failed") as failure:
            integration_helpers.internxt("whoami")
    assert "PRIVATE_" not in str(failure.value)


def test_npm_prefix_is_recorded_for_windows_and_linux_installations():
    command = [r"C:\node\node.exe", r"C:\node\node_modules\@internxt\cli\bin\run.js"]
    with mock.patch.object(backup_module.platform, "system", return_value="Windows"):
        assert backup_module.resolve_npm_prefix(command) == r"C:\node"

    completed = backup_module.subprocess.CompletedProcess(["npm"], 0, "/usr/local\n", "")
    with mock.patch.object(backup_module.platform, "system", return_value="Linux"), \
         mock.patch.object(backup_module.shutil, "which", return_value="/usr/bin/npm"), \
         mock.patch.object(backup_module.subprocess, "run", return_value=completed) as run:
        assert backup_module.resolve_npm_prefix(["/usr/bin/internxt"]) == "/usr/local"
    assert run.call_args.args[0] == ["/usr/bin/npm", "prefix", "-g"]
    assert run.call_args.kwargs["shell"] is False


def test_uuidv7_version_gate_and_permanent_validation_error_once(tmp_path):
    # Preserve the UUIDv7 validation regression with a synthetic, non-account identifier.
    v7 = "00000000-0000-7000-8000-000000000001"
    assert backup_module.validate_uuid(v7, "test") == v7
    with pytest.raises(backup_module.BackupError):
        backup_module.check_cli_version("@internxt/cli/1.6.3 win32-x64 node-v22.18.0")
    assert backup_module.classify_error("502 Bad Gateway") == "transient"
    assert backup_module.classify_error("Folder UUID is not valid (it must be a valid v4 UUID)") == "permanent"
    assert backup_module.classify_error("SqliteError: attempt to write a readonly database") == "permanent"

    class ScriptedRunner:
        max_attempts = 5
        def __init__(self, messages):
            self.messages = list(messages)
            self.calls = 0
        def once(self, *_args, **_kwargs):
            self.calls += 1
            value = self.messages.pop(0)
            return value

    source = tmp_path / "src"
    source.mkdir()
    remote = FakeRemote()
    args = make_args(remote)
    logger = logging.getLogger(f"retry-test-{uuid.uuid4()}")
    logger.addHandler(logging.NullHandler())
    progress = backup_module.Progress(time.monotonic(), 1, enabled=False)
    local = backup_module.scan_local_tree(source, progress, logger)
    retry_then_success = ScriptedRunner([
        backup_module.CommandResult(False, None, "502 Bad Gateway", 1, 0.01),
        backup_module.CommandResult(True, {"success": True, "list": {"files": [], "folders": []}}, "", 0, 0.01),
    ])
    backup = backup_module.Backup(args=args, runner=retry_then_success, logger=logger, progress=progress,
                                  cancel_event=threading.Event(), source_root=source, source_name="src",
                                  copy_contents=False, local_root=local)
    backup.retry_wait = lambda *_args: setattr(backup.stats, "retry_count", backup.stats.retry_count + 1)
    assert backup.read_command(["list", f"--id={remote.root_uuid}"], "list", "test")["success"]
    assert retry_then_success.calls == 2

    permanent = ScriptedRunner([
        backup_module.CommandResult(False, None, "Folder UUID is not valid (it must be a valid v4 UUID)", 1, 0.01),
    ])
    backup.runner = permanent
    with pytest.raises(backup_module.BranchError):
        backup.read_command(["list", "--id=bad"], "list", "bad")
    assert permanent.calls == 1

    mixed = ScriptedRunner([
        backup_module.CommandResult(False, None, "502 Bad Gateway", 1, 0.01),
        backup_module.CommandResult(False, None, "Folder UUID is not valid (it must be a valid v4 UUID)", 1, 0.01),
    ])
    backup.runner = mixed
    with pytest.raises(backup_module.BranchError, match=r"failed after 2 attempt\(s\)"):
        backup.read_command(["list", "--id=bad"], "list", "bad")
    assert mixed.calls == 2


def test_names_sizes_duplicates_and_collisions_are_strict():
    assert backup_module.remote_item_name({"plainName": "literal%20name", "type": "txt"}, "file") == "literal%20name.txt"
    assert backup_module.remote_item_name({"plainName": "file", "type": "folder"}, "file") == "file.folder"
    assert backup_module.remote_item_name({"plainName": "extensionless", "type": None}, "file") == "extensionless"
    assert backup_module.remote_item_name({"plainName": "Käfer 雪", "type": "tar.gz"}, "file") == "Käfer 雪.tar.gz"
    valid = {"success": True, "list": {"files": [{"uuid": new_uuid(), "plainName": "name", "type": "txt", "size": "257"}], "folders": []}}
    assert backup_module.validate_listing(valid).files["name.txt"].size == 257
    for malformed in (
        {"success": True},
        {"success": True, "list": {"files": {}, "folders": []}},
        {"success": True, "list": {"files": [{"uuid": new_uuid(), "type": "txt", "size": 1}], "folders": []}},
        {"success": True, "list": {"files": [{"uuid": "no", "plainName": "x", "type": None, "size": 1}], "folders": []}},
        {"success": True, "list": {"files": [{"uuid": new_uuid(), "plainName": "x", "type": None, "size": -1}], "folders": []}},
    ):
        with pytest.raises(backup_module.BranchError):
            backup_module.validate_listing(malformed)
    shared = new_uuid()
    with pytest.raises(backup_module.BranchError):
        backup_module.validate_listing({"success": True, "list": {
            "files": [{"uuid": shared, "plainName": "same", "type": "txt", "size": 1},
                      {"uuid": new_uuid(), "plainName": "same", "type": "txt", "size": 2}],
            "folders": [],
        }})
    with pytest.raises(backup_module.BranchError):
        backup_module.validate_listing({"success": True, "list": {
            "files": [{"uuid": new_uuid(), "plainName": "same", "type": None, "size": 1}],
            "folders": [{"uuid": new_uuid(), "plainName": "same"}],
        }})


def test_prescan_reuses_listings_and_new_folders_start_empty(tmp_path):
    source = tmp_path / "backup"
    leaf = source / "already" / "nested"
    leaf.mkdir(parents=True)
    (leaf / "same.txt").write_text("same", encoding="utf-8")
    (source / "new" / "branch").mkdir(parents=True)
    (source / "new" / "branch" / "fresh.bin").write_bytes(b"new")

    list_uuid_sequences = []
    statuses = []
    for no_prescan in (False, True):
        remote = FakeRemote()
        root = remote.add_folder(remote.root_uuid, "backup")
        already = remote.add_folder(root.uuid, "already")
        nested = remote.add_folder(already.uuid, "nested")
        remote.add_file(nested.uuid, "same.txt", 4)
        backup, status, _ = make_backup(source, remote, no_prescan=no_prescan)
        list_calls = [call[0][1].split("=", 1)[1] for call in remote.calls if call[0][0] == "list"]
        list_uuid_sequences.append(list_calls)
        statuses.append(status)
        assert backup.progress.folders_checked == backup.total_folders
        assert backup.stats.files_uploaded == 1
        assert backup.stats.files_skipped_same_size == 1
        assert len(list_calls) == len(set(list_calls))
        new_uuid_value = remote.directories[root.uuid].folders["new"].uuid
        branch_uuid_value = remote.directories[new_uuid_value].folders["branch"].uuid
        assert new_uuid_value not in list_calls and branch_uuid_value not in list_calls
    assert statuses == [0, 0]
    assert len(list_uuid_sequences[0]) == len(list_uuid_sequences[1]) == 4


def test_renamed_folder_preserves_uuid_and_does_not_upload_or_delete(tmp_path):
    source = tmp_path / "backup"
    renamed = source / "new-name"
    renamed.mkdir(parents=True)
    (renamed / "same.txt").write_text("same", encoding="utf-8")
    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, "backup")
    old = remote.add_folder(root.uuid, "old-name")
    remote.add_file(old.uuid, "same.txt", 4)

    backup, status, _ = make_backup(source, remote, allow_delete=True)

    assert status == 0
    assert backup.stats.folders_renamed == 1
    assert backup.stats.files_uploaded == 0
    assert backup.stats.files_deleted == 0
    final = remote.directories[root.uuid].folders["new-name"]
    assert final.uuid == old.uuid
    assert "old-name" not in remote.directories[root.uuid].folders
    list_uuids = [call[0][1].split("=", 1)[1] for call in remote.calls if call[0][0] == "list"]
    assert list_uuids.count(old.uuid) == 1


def test_lost_upload_response_and_file_already_exists_reconcile_once(tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    (source / "new.txt").write_text("payload", encoding="utf-8")
    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, "backup")

    def lost_response(_remote, _parent, _path, _item, _result):
        return _remote.result(None, False, "request timed out")

    remote.next_upload = lost_response
    backup, status, _ = make_backup(source, remote)
    assert status == 0
    assert backup.stats.files_reconciled == 1
    assert len([c for c in remote.calls if c[0][0] == "upload-file"]) == 1
    assert "new.txt" in remote.directories[root.uuid].files

    source2 = tmp_path / "conflict-visible"
    source2.mkdir()
    (source2 / "visible.txt").write_text("payload", encoding="utf-8")
    remote2 = FakeRemote()
    root2 = remote2.add_folder(remote2.root_uuid, "conflict-visible")
    def conflict_after_creation(_remote, _parent, _path, _item, _result):
        return _remote.result(None, False, "File already exists")
    remote2.next_upload = conflict_after_creation
    reconciled, status2, _ = make_backup(source2, remote2)
    assert status2 == 0
    assert reconciled.stats.files_reconciled == 1
    assert len([c for c in remote2.calls if c[0][0] == "upload-file"]) == 1
    assert "visible.txt" in remote2.directories[root2.uuid].files


def test_success_response_with_delayed_listing_visibility_does_not_reupload(tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    (source / "delayed.txt").write_text("payload", encoding="utf-8")
    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, source.name)

    def hide_once(remote_instance, _parent, _path, _item, _result):
        def hide_first_list(_remote, _folder_uuid, listing_result):
            listing_result.data["list"]["files"] = []
            return listing_result
        remote_instance.next_list = hide_first_list
        # The CLI said success but omitted the file object, so identity must be reconciled.
        return remote_instance.result({"success": True, "message": "Upload accepted"})

    remote.next_upload = hide_once
    backup, status, _ = make_backup(source, remote)
    assert status == 0
    assert backup.stats.files_reconciled == 1
    assert len([call for call in remote.calls if call[0][0] == "upload-file"]) == 1
    assert remote.directories[root.uuid].files["delayed.txt"].size == 7


def test_delayed_visibility_retries_transient_but_conflict_never_blindly_reuploads(tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    (source / "new.txt").write_text("payload", encoding="utf-8")
    remote = FakeRemote()
    remote.add_folder(remote.root_uuid, "backup")

    def delayed(_remote, _parent, _path, _item, _result):
        _remote.directories[_parent].remove_uuid(_item.uuid)
        return _remote.result(None, False, "502 Bad Gateway")

    remote.next_upload = delayed
    backup, status, _ = make_backup(source, remote)
    # The first response is a transient error; the reconciler confirms absence and the next upload succeeds.
    # The callback is one-shot, so the second attempt uses normal fake CLI behavior.
    assert status == 0
    assert len([c for c in remote.calls if c[0][0] == "upload-file"]) == 2

    conflict_source = tmp_path / "conflict-backup"
    conflict_source.mkdir()
    (conflict_source / "already.txt").write_text("payload", encoding="utf-8")
    remote2 = FakeRemote()
    remote2.add_folder(remote2.root_uuid, "conflict-backup")
    def persistent_conflict(_remote, _parent, _path, _item, _result):
        # Simulate a CLI name conflict whose object is not visible in listings.
        listing = _remote.directories[_parent]
        listing.remove_uuid(_item.uuid)
        return _remote.result(None, False, "File already exists")
    remote2.next_upload = persistent_conflict
    failed, failed_status, _ = make_backup(conflict_source, remote2)
    assert failed_status == 1
    assert len([c for c in remote2.calls if c[0][0] == "upload-file"]) == 1
    assert failed.stats.files_failed == 1
    assert "Backup completed" not in _


def test_replacement_uses_overwrite_and_preserves_old_file_on_failure(tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    (source / "replace.txt").write_text("new bytes", encoding="utf-8")
    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, "backup")
    old = remote.add_file(root.uuid, "replace.txt", 3)

    def failed_overwrite(_remote, _parent, _path, _item, _result):
        _remote.directories[_parent].remove_uuid(_item.uuid)
        _remote.directories[_parent].put(old)
        return _remote.result(None, False, "Account storage limit reached")
    remote.next_upload = failed_overwrite
    backup, status, _ = make_backup(source, remote, allow_delete=True)
    upload_commands = [call[0] for call in remote.calls if call[0][0] == "upload-file"]
    assert status == 1
    assert upload_commands == [["upload-file", "--file", str(source / "replace.txt"), f"--destination={root.uuid}", "--overwrite"]]
    assert not any(call[0][0] == "delete-permanently-file" for call in remote.calls)
    assert remote.directories[root.uuid].files["replace.txt"].uuid == old.uuid


def test_mismatch_without_delete_is_an_explicit_policy_skip(tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    (source / "replace.txt").write_text("new bytes", encoding="utf-8")
    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, "backup")
    old = remote.add_file(root.uuid, "replace.txt", 3)
    backup, status, _ = make_backup(source, remote, allow_delete=False)
    assert status == 0
    assert backup.stats.files_skipped_mismatch == 1
    assert not any(call[0][0] == "upload-file" for call in remote.calls)
    assert remote.directories[root.uuid].files["replace.txt"].uuid == old.uuid


def test_incomplete_upload_is_not_success_and_overwrite_requires_authorization(tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    (source / "partial.bin").write_bytes(b"complete")

    def partial_response(remote, parent, _path, item, _result):
        remote.directories[parent].remove_uuid(item.uuid)
        remote.directories[parent].put(remote_file("partial.bin", 3))
        return remote.result(None, False, "Size does not match")

    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, "backup")
    remote.next_upload = partial_response
    failed, status, _output = make_backup(source, remote, allow_delete=False)
    assert status == 1
    assert failed.stats.files_failed == 1
    assert remote.directories[root.uuid].files["partial.bin"].size == 3
    assert len([call for call in remote.calls if call[0][0] == "upload-file"]) == 1
    assert not any("--overwrite" in call[0] for call in remote.calls if call[0][0] == "upload-file")

    remote2 = FakeRemote()
    root2 = remote2.add_folder(remote2.root_uuid, "backup")
    remote2.next_upload = partial_response
    repaired, repaired_status, _ = make_backup(source, remote2, allow_delete=True)
    commands = [call[0] for call in remote2.calls if call[0][0] == "upload-file"]
    assert repaired_status == 0
    assert commands[0][-1] != "--overwrite" and commands[1][-1] == "--overwrite"
    assert remote2.directories[root2.uuid].files["partial.bin"].size == 8
    assert repaired.stats.files_replaced == 0  # it repaired an uncertain new upload, not a planned replacement
    assert not any(call[0][0] == "delete-permanently-file" for call in remote2.calls)


def test_zero_byte_and_over_limit_files_have_truthful_totals(tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    (source / "empty").write_bytes(b"")
    remote = FakeRemote()
    remote.add_folder(remote.root_uuid, "backup")
    backup, status, output = make_backup(source, remote)
    assert status == 0
    assert backup.progress.planned_uploads == 1
    assert backup.progress.planned_bytes == 0
    assert backup.stats.upload_bytes == 0
    assert "Backup completed under the selected size-based policy" in output

    large_source = tmp_path / "large-source"
    large_source.mkdir()
    (large_source / "one-byte").write_bytes(b"x")
    large_remote = FakeRemote()
    large_remote.add_folder(large_remote.root_uuid, "large-source")
    with mock.patch.object(backup_module, "FILE_SIZE_UPLOAD_LIMIT_BYTES", 0):
        too_large, large_status, large_output = make_backup(large_source, large_remote)
    assert large_status == 1
    assert too_large.stats.files_too_large == 1
    assert too_large.stats.upload_bytes == 0
    assert "Backup completed" not in large_output
    assert not any(call[0][0] == "upload-file" for call in large_remote.calls)


def test_unreadable_branch_is_reported_while_independent_work_continues(tmp_path):
    source = tmp_path / "backup"
    blocked = source / "blocked"
    good = source / "good"
    blocked.mkdir(parents=True)
    good.mkdir()
    (blocked / "hidden.txt").write_text("hidden", encoding="utf-8")
    (good / "visible.txt").write_text("visible", encoding="utf-8")
    real_scandir = backup_module.os.scandir

    def failing_scandir(path):
        if Path(path) == blocked:
            raise PermissionError("test unreadable branch")
        return real_scandir(path)

    remote = FakeRemote()
    remote.add_folder(remote.root_uuid, "backup")
    with mock.patch.object(backup_module.os, "scandir", side_effect=failing_scandir):
        backup, status, _output = make_backup(source, remote)
    assert status == 1
    assert any(call[0][0] == "upload-file" and Path(call[0][2]).name == "visible.txt" for call in remote.calls)
    assert not any(call[0][0] == "upload-file" and Path(call[0][2]).name == "hidden.txt" for call in remote.calls)
    assert backup.stats.files_uploaded == 1


def test_cli_timeout_kills_owned_process_and_retry_progress_shows_countdown(tmp_path):
    class StuckProcess:
        pid = 4321
        returncode = None
        reaped = False
        def poll(self):
            return self.returncode
        def kill(self):
            self.returncode = -9
        def communicate(self, timeout=None):
            if timeout is not None and self.returncode is None:
                raise backup_module.subprocess.TimeoutExpired("fake", timeout)
            self.reaped = True
            return "", ""

    process = StuckProcess()
    logger = logging.getLogger(f"timeout-test-{uuid.uuid4()}")
    logger.addHandler(logging.NullHandler())
    progress = backup_module.Progress(0, 1, enabled=False)
    runner = backup_module.CliRunner(["fake-node", "cli.js"], logger, progress, threading.Event())
    clock = iter((0.0, 0.0, 2.0, 2.0))
    with mock.patch.object(backup_module.subprocess, "Popen", return_value=process), \
         mock.patch.object(backup_module.platform, "system", return_value="Windows"), \
         mock.patch.object(backup_module.time, "monotonic", side_effect=lambda: next(clock)), \
         mock.patch.object(runner, "_terminate_tree", side_effect=lambda child: child.kill()):
        result = runner.once(["list"], operation="list", path="blocked", attempt=1, timeout=1)
    assert result.timed_out is True
    assert process.returncode == -9 and process.reaped

    source = tmp_path / "retry"
    source.mkdir()
    remote = FakeRemote()
    backup, _status, _output = make_backup(source, remote)
    backup.cancel_event.set()
    backup.progress.enabled = True
    backup.progress.tty = False
    retry_output = io.StringIO()
    with contextlib.redirect_stdout(retry_output), pytest.raises(backup_module.CancelledError):
        backup.retry_wait("list", 1, "retry/path")
    assert "retry in" in retry_output.getvalue()


def test_failed_branch_blocks_ancestor_cleanup_but_sibling_cleanup_runs(tmp_path):
    source = tmp_path / "backup"
    failed_dir = source / "failed"
    good_dir = source / "good"
    failed_dir.mkdir(parents=True)
    good_dir.mkdir()
    (failed_dir / "new.txt").write_text("new", encoding="utf-8")
    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, "backup")
    failed = remote.add_folder(root.uuid, "failed")
    good = remote.add_folder(root.uuid, "good")
    remote.add_file(failed.uuid, "old.txt", 3)
    remote.add_file(good.uuid, "old.txt", 3)

    def always_fail(remote_instance, args, *, operation, path, attempt, timeout):
        if args[0] == "upload-file":
            remote_instance.calls.append((list(args), operation, path, attempt, timeout))
            return remote_instance.result(None, False, "Account storage limit reached")
        return original_once(args, operation=operation, path=path, attempt=attempt, timeout=timeout)
    original_once = remote.once
    remote.once = lambda args, **kwargs: always_fail(remote, args, **kwargs)

    backup, status, _ = make_backup(source, remote, allow_delete=True)
    assert status == 1
    assert "old.txt" in remote.directories[failed.uuid].files
    assert "old.txt" not in remote.directories[good.uuid].files
    assert backup.stats.files_deleted == 1


def test_uncertain_folder_create_rename_and_delete_reconcile_by_identity(tmp_path):
    source = tmp_path / "create-backup"
    nested = source / "new-folder" / "nested"
    nested.mkdir(parents=True)
    (nested / "payload.txt").write_text("payload", encoding="utf-8")
    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, source.name)

    def lost_create_response(_remote, _parent, _name, _item, _result):
        return _remote.result(None, False, "502 Bad Gateway")

    remote.next_create = lost_create_response
    backup, status, _ = make_backup(source, remote)
    assert status == 0
    assert backup.stats.other_reconciled_outcomes == 1
    assert len([call for call in remote.calls if call[0][0] == "create-folder"]) == 2
    created = remote.directories[root.uuid].folders["new-folder"]
    assert "nested" in remote.directories[created.uuid].folders

    rename_source = tmp_path / "rename-backup"
    (rename_source / "new-name").mkdir(parents=True)
    (rename_source / "new-name" / "same.txt").write_text("same", encoding="utf-8")
    rename_remote = FakeRemote()
    rename_root = rename_remote.add_folder(rename_remote.root_uuid, rename_source.name)
    old = rename_remote.add_folder(rename_root.uuid, "old-name")
    rename_remote.add_file(old.uuid, "same.txt", 4)

    def lost_rename_response(_remote, _parent, _item, _result):
        return _remote.result(None, False, "request timed out")

    rename_remote.next_rename = lost_rename_response
    renamed, rename_status, _ = make_backup(rename_source, rename_remote, allow_delete=True)
    assert rename_status == 0
    assert renamed.stats.folders_renamed == 1
    assert renamed.stats.other_reconciled_outcomes == 1
    assert rename_remote.directories[rename_root.uuid].folders["new-name"].uuid == old.uuid

    delete_source = tmp_path / "delete-backup"
    delete_source.mkdir()
    delete_remote = FakeRemote()
    delete_root = delete_remote.add_folder(delete_remote.root_uuid, delete_source.name)
    old_file = delete_remote.add_file(delete_root.uuid, "remote-only.bin", 12)

    def lost_delete_response(_remote, _parent, _item, _result):
        return _remote.result(None, False, "502 Bad Gateway")

    delete_remote.next_delete = lost_delete_response
    deleted, delete_status, _ = make_backup(delete_source, delete_remote, allow_delete=True)
    assert delete_status == 0
    assert deleted.stats.files_deleted == 1
    assert deleted.stats.other_reconciled_outcomes == 1
    assert old_file.uuid not in {item.uuid for item in delete_remote.directories[delete_root.uuid].files.values()}


def test_delete_reconciliation_never_deletes_a_same_name_replacement(tmp_path):
    source = tmp_path / "delete-backup"
    source.mkdir()
    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, source.name)
    old = remote.add_file(root.uuid, "remote-only.bin", 12)

    def replace_after_lost_response(remote_instance, parent, item, _result):
        replacement = remote_file(item.name, item.size)
        remote_instance.directories[parent].put(replacement)
        return remote_instance.result(None, False, "request timed out")

    remote.next_delete = replace_after_lost_response
    backup, status, _ = make_backup(source, remote, allow_delete=True)
    assert status == 1
    current = remote.directories[root.uuid].files["remote-only.bin"]
    assert current.uuid != old.uuid
    assert backup.stats.files_deleted == 0
    assert any("different UUID with the same name" in failure for failure in backup.stats.failures)


def test_auth_failure_is_terminal_and_secrets_are_redacted():
    class AuthRunner:
        calls = 0
        def once(self, *_args, **_kwargs):
            self.calls += 1
            return backup_module.CommandResult(False, None, "Unauthorized", 1, 0.01)

    source = Path(__file__).resolve().parent
    logger = logging.getLogger(f"auth-test-{uuid.uuid4()}")
    logger.addHandler(logging.NullHandler())
    progress = backup_module.Progress(time.monotonic(), 1, enabled=False)
    local = backup_module.LocalDirectory(source, source.name, ".", "", (), 0)
    backup = backup_module.Backup(args=make_args(FakeRemote()),
                                  runner=AuthRunner(), logger=logger, progress=progress,
                                  cancel_event=threading.Event(), source_root=source,
                                  source_name=source.name, copy_contents=False, local_root=local)
    with pytest.raises(backup_module.AuthenticationError):
        backup.read_command(["list", "--id=abc"], "list", ".")
    assert backup.runner.calls == 1

    sanitized = backup_module.error_message(
        None, "", "Authorization: Bearer super-secret-token password=hunter2", "fallback"
    )
    assert "super-secret-token" not in sanitized
    assert "hunter2" not in sanitized
    json_message = backup_module.error_message(
        {"message": "request failed; access_token=secret-value"}, "", "", "fallback"
    )
    assert "secret-value" not in json_message


def test_whoami_payload_is_never_logged_and_cli_process_uses_no_shell():
    class SuccessfulProcess:
        pid = 4322
        returncode = 0
        def poll(self):
            return 0
        def communicate(self, timeout=None):
            return json.dumps({"success": True, "login": {"token": "private-token", "user": {"password": "secret-password"}}}), ""

    process = SuccessfulProcess()
    log_output = io.StringIO()
    logger = logging.getLogger(f"whoami-log-{uuid.uuid4()}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(log_output)
    logger.addHandler(handler)
    progress = backup_module.Progress(0.0, 0, enabled=False)
    runner = backup_module.CliRunner(["node", "cli.js"], logger, progress, threading.Event())
    with mock.patch.object(backup_module.platform, "system", return_value="Linux"), \
         mock.patch.object(backup_module.subprocess, "Popen", return_value=process) as popen:
        result = runner.once(["whoami"], operation="whoami", path="preflight", attempt=1, timeout=5)
    assert result.success is True
    assert "private-token" not in log_output.getvalue()
    assert "secret-password" not in log_output.getvalue()
    assert popen.call_args.kwargs["shell"] is False


def test_ignore_behavior_and_source_changes_are_conservative(tmp_path):
    source = tmp_path / "backup"
    ignored = source / "ignored"
    kept = source / "kept"
    ignored.mkdir(parents=True)
    kept.mkdir()
    (ignored / ".internxtignore").write_text("", encoding="utf-8")
    (ignored / "secret.txt").write_text("secret", encoding="utf-8")
    (kept / "visible.txt").write_text("visible", encoding="utf-8")
    (source / ".internxtignore").write_text("root marker remains a file", encoding="utf-8")

    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, "backup")
    remote_ignored = remote.add_folder(root.uuid, "ignored")
    remote.add_file(remote_ignored.uuid, "secret.txt", 6)
    backup, status, _ = make_backup(source, remote, allow_delete=False)
    assert status == 0
    assert backup.stats.files_uploaded == 2  # visible.txt and the source-root marker
    assert "ignored" in remote.directories[root.uuid].folders

    remote2 = FakeRemote()
    root2 = remote2.add_folder(remote2.root_uuid, "backup")
    ignored2 = remote2.add_folder(root2.uuid, "ignored")
    remote2.add_file(ignored2.uuid, "secret.txt", 6)
    backup2, status2, _ = make_backup(source, remote2, allow_delete=True)
    assert status2 == 0
    assert "ignored" not in remote2.directories[root2.uuid].folders

    changed_source = tmp_path / "changed-source"
    changed_source.mkdir()
    changed_file = changed_source / "visible.txt"
    changed_file.write_text("before", encoding="utf-8")
    remote3 = FakeRemote()
    remote3.add_folder(remote3.root_uuid, "changed-source")
    progress = backup_module.Progress(time.monotonic(), 1, enabled=False)
    logger = logging.getLogger(f"source-change-{uuid.uuid4()}")
    logger.addHandler(logging.NullHandler())
    local = backup_module.scan_local_tree(changed_source, progress, logger)
    args = make_args(remote3)
    backup3 = backup_module.Backup(args=args, runner=remote3, logger=logger, progress=progress,
                                   cancel_event=threading.Event(), source_root=changed_source, source_name="changed-source",
                                   copy_contents=False, local_root=local)
    changed_file.write_text("changed while planned", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        status3 = backup3.process()
    assert status3 == 1
    assert not any(call[0][0] == "upload-file" for call in remote3.calls)


def test_source_change_after_a_sibling_finishes_blocks_its_deferred_cleanup(tmp_path):
    source = tmp_path / "backup"
    first = source / "a"
    later = source / "z"
    first.mkdir(parents=True)
    later.mkdir()
    changing_file = first / "stable.txt"
    changing_file.write_text("ok", encoding="utf-8")
    (later / "trigger.txt").write_text("later", encoding="utf-8")

    remote = FakeRemote()
    root = remote.add_folder(remote.root_uuid, "backup")
    first_remote = remote.add_folder(root.uuid, "a")
    remote.add_file(first_remote.uuid, "stable.txt", 2)
    extra = remote.add_file(first_remote.uuid, "remote-only.txt", 5)
    remote.add_folder(root.uuid, "z")

    def mutate_completed_sibling(remote_instance, _parent, _path, _item, result):
        changing_file.write_text("changed after the first branch completed", encoding="utf-8")
        return result

    remote.next_upload = mutate_completed_sibling
    backup, status, _ = make_backup(source, remote, allow_delete=True)
    assert status == 1
    assert any("Source file changed" in failure for failure in backup.stats.failures)
    assert remote.directories[first_remote.uuid].files["remote-only.txt"].uuid == extra.uuid


def test_progress_heartbeat_and_process_tree_termination_are_visible():
    progress = backup_module.Progress(0.0, total_folders=1, enabled=True)
    progress.tty = False
    progress.operation = "list"
    progress.operation_started = 0.0
    progress.attempt = 2
    progress.max_attempts = 5
    progress.current_path = "large folder"
    output = io.StringIO()
    with contextlib.redirect_stdout(output), mock.patch.object(backup_module.time, "monotonic", return_value=15.0):
        progress.last_render = 0.0
        progress.tick()
    line = output.getvalue()
    assert "list" in line and "attempt 2/5" in line and "large folder" in line

    scanning = backup_module.Progress(0.0, total_folders=None, enabled=True)
    scanning.tty = False
    scanning.folders_checked = 2695
    scanning.current_path = "catalog/large-directory"
    scan_output = io.StringIO()
    with contextlib.redirect_stdout(scan_output), mock.patch.object(backup_module.time, "monotonic", return_value=15.0):
        scanning.tick()
    assert "folders scanned 2695" in scan_output.getvalue()
    assert "folders 0/0" not in scan_output.getvalue()

    class StuckProcess:
        pid = 1234
        returncode = None
        killed = False
        def poll(self):
            return self.returncode
        def kill(self):
            self.killed = True
            self.returncode = -9
        def communicate(self, timeout=None):
            if timeout is not None and not self.killed:
                raise backup_module.subprocess.TimeoutExpired("fake", timeout)
            return "", ""
    process = StuckProcess()
    cancel = threading.Event()
    logger = logging.getLogger(f"timeout-{uuid.uuid4()}")
    logger.addHandler(logging.NullHandler())
    cli_progress = backup_module.Progress(time.monotonic(), 1, enabled=False)
    runner = backup_module.CliRunner(["fake-node", "cli.js"], logger, cli_progress, cancel)
    def cancel_after_spawn(*_args, **_kwargs):
        cancel.set()
        return process
    with mock.patch.object(backup_module.subprocess, "Popen", side_effect=cancel_after_spawn), \
         mock.patch.object(backup_module.platform, "system", return_value="Windows"), \
         mock.patch.object(runner, "_terminate_tree") as terminate, \
         pytest.raises(backup_module.CancelledError):
        runner.once(["list"], operation="list", path="folder", attempt=1, timeout=10)
    terminate.assert_called_once_with(process)


# Synthetic JWTs exercise scheduling only; no test changes or expires the user's actual login.
def session_token(issued, expires):
    claims = {"exp": expires}
    if issued is not None:
        claims["iat"] = issued
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"success": True, "login": {"token": "header." + payload + ".signature"}}


def test_token_window_matches_cli_and_rejects_unusable_claims():
    assert backup_module.session_refresh_at(session_token(1000, 9000)) == 5000
    assert backup_module.session_refresh_at(session_token(None, 30000)) == 8400
    for session in ({"success": True}, {"login": {"token": "invalid"}},
                    session_token(9000, 1000), session_token(True, 9000),
                    session_token(1000, float("inf")), session_token(1000, "9000"), session_token(1000, 10 ** 400)):
        assert backup_module.session_refresh_at(session) is None


def test_parallel_gate_refreshes_serially_and_caches_only_verified_window():
    wall, steady = [1000.0], [10.0]
    stale = session_token(0, 1500)
    fresh = session_token(1000, 9000)
    reader = mock.Mock(side_effect=[stale, fresh])
    gate = backup_module.ParallelUploadGate(reader, logging.getLogger("gate-test"),
                                           lambda: wall[0], lambda: steady[0])
    gate.observe(stale)
    assert gate.prepare(1800)
    assert reader.call_count == 2  # whoami's stale return after refresh must not grant permission
    assert gate.prepare(1800)
    assert reader.call_count == 2  # ordinary batches do not add whoami/list calls
    assert not gate.prepare(5000)  # an oversized deadline runs alone without needless refresh calls
    assert reader.call_count == 2
    wall[0] = 0  # clock rollback cannot extend an already granted window
    steady[0] = 3900
    assert not gate.allows(100)
    wall[0] = 6000
    assert not gate.allows(1)


def test_failed_hidden_refresh_disables_parallelism_without_logging_secrets(caplog):
    stale = session_token(0, 1500)
    reader = mock.Mock(return_value=stale)
    logger = logging.getLogger("failed-gate-test")
    gate = backup_module.ParallelUploadGate(reader, logger, lambda: 1000.0, lambda: 10.0)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert not gate.prepare(10)
        assert not gate.prepare(10)
    assert reader.call_count == 2
    assert gate.disabled
    assert stale["login"]["token"] not in caplog.text
    assert "sequential uploads" in caplog.text
    auth_reader = mock.Mock(side_effect=backup_module.AuthenticationError("manual login needed"))
    auth_gate = backup_module.ParallelUploadGate(auth_reader, logger)
    with pytest.raises(backup_module.AuthenticationError):
        auth_gate.prepare(10)


# The fake backend synchronizes both first attempts, and asserts all metadata/recovery work runs after they drain.
@pytest.mark.parametrize("no_prescan", [False, True])
@pytest.mark.parametrize("cross_refresh", [False, True])
@pytest.mark.parametrize("uncertain", ["lost_success", "not_committed"])
def test_parallel_uploads_keep_recovery_cache_and_replacement_serial(tmp_path, no_prescan, cross_refresh, uncertain):
    class ParallelRemote(FakeRemote):
        def __init__(self):
            super().__init__()
            self.lock = threading.Lock()
            self.barrier = threading.Barrier(2)
            self.active = 0
            self.peak = 0
            self.owner = threading.get_ident()
            self.whoami_reads = 0
            self.wall = 1000.0
            self.steady = 10.0

        def once(self, args, **kwargs):
            if args[0] == "whoami":
                assert self.active == 0 and threading.get_ident() == self.owner
                self.whoami_reads += 1
                return self.result(session_token(1000, 2000) if self.whoami_reads < 3 else session_token(1500, 4000))
            parallel = kwargs.get("report_progress") is False
            if parallel:
                assert args[0] == "upload-file" and kwargs["attempt"] == 1
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                try:
                    self.barrier.wait(timeout=3)
                    with self.lock:
                        result = super().once(args, **kwargs)
                    if Path(args[2]).name == "a.bin":
                        if uncertain == "not_committed":
                            with self.lock:
                                self.directories[args[3].split("=", 1)[1]].remove_uuid(result.data["file"]["uuid"])
                        return self.result(None, False, "write ECONNRESET")  # committed remotely, response lost
                    return result
                finally:
                    with self.lock:
                        self.active -= 1
                        if cross_refresh and self.active == 0 and len([c for c in self.calls if c[0][0] == "upload-file"]) == 2:
                            self.wall, self.steady = 1500.0, 510.0
            assert self.active == 0 and threading.get_ident() == self.owner
            return super().once(args, **kwargs)

    source = tmp_path / "parallel"
    source.mkdir()
    for name in ("a.bin", "b.bin", "c.bin", "d.bin"):
        (source / name).write_bytes(b"new payload")
    remote = ParallelRemote()
    root = remote.add_folder(remote.root_uuid, source.name)
    remote.add_file(root.uuid, "b.bin", 1)
    gate_type = backup_module.ParallelUploadGate
    def gate_factory(reader, logger):
        return gate_type(reader, logger, lambda: remote.wall, lambda: remote.steady)
    with mock.patch.object(backup_module, "ParallelUploadGate", side_effect=gate_factory):
        backup, status, _ = make_backup(source, remote, allow_delete=True, no_prescan=no_prescan, upload_workers=2)
    assert status == 0
    assert remote.peak == 2 and remote.active == 0
    assert remote.whoami_reads == (3 if cross_refresh else 1)
    assert backup.stats.files_uploaded == 4 and backup.stats.files_replaced == 1
    assert backup.stats.files_reconciled == (1 if uncertain == "lost_success" else 0)
    assert backup.stats.reconciliation_reads == 1
    assert backup.stats.retry_count == (1 if uncertain == "not_committed" else 0)
    uploads = [args for args, *_ in remote.calls if args[0] == "upload-file"]
    assert len(uploads) == (4 if uncertain == "lost_success" else 5)
    assert [call[3] for call in remote.calls if call[0][0] == "upload-file" and Path(call[0][2]).name == "a.bin"] == (
        [1] if uncertain == "lost_success" else [1, 2])
    assert [Path(args[2]).name for args in uploads if "--overwrite" in args] == ["b.bin"]
    assert not any(call[0][0].startswith("delete") for call in remote.calls)
    assert set(remote.directories[root.uuid].files) == {"a.bin", "b.bin", "c.bin", "d.bin"}


def test_unverified_refresh_runs_the_whole_backup_sequentially(tmp_path):
    class StaleRemote(FakeRemote):
        reads = 0
        def once(self, args, **kwargs):
            assert kwargs.get("report_progress") is not False
            if args[0] == "whoami":
                self.reads += 1
                return self.result(session_token(time.time() - 7200, time.time() + 60))
            return super().once(args, **kwargs)
    source = tmp_path / "fallback"
    source.mkdir()
    for index in range(4):
        (source / f"{index}.bin").write_bytes(b"data")
    remote = StaleRemote()
    remote.add_folder(remote.root_uuid, source.name)
    backup, status, _ = make_backup(source, remote, upload_workers=2)
    assert status == 0 and backup.stats.files_uploaded == 4
    assert remote.reads == 2 and backup.upload_gate.disabled


def test_preexisting_cancellation_never_starts_another_cli():
    cancel = threading.Event()
    cancel.set()
    progress = backup_module.Progress(time.monotonic(), 1, enabled=False)
    runner = backup_module.CliRunner(["unused"], logging.getLogger("cancel-test"), progress, cancel)
    with mock.patch.object(backup_module.subprocess, "Popen") as spawn:
        with pytest.raises(backup_module.CancelledError):
            runner.once(["upload-file"], operation="upload-file", path="x", attempt=1, timeout=10)
    spawn.assert_not_called()


# Real local subprocesses verify that both workers are reaped; no CLI installation or account is involved.
@pytest.mark.parametrize("mode", ["timeout", "cancel", "auth"])
def test_parallel_processes_stop_on_deadline_cancel_and_auth_failure(tmp_path, mode):
    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "name=Path(sys.argv[sys.argv.index('--file')+1]).name\n"
        "if sys.argv[1]=='auth' and name=='a.bin':\n"
        " print(json.dumps({'success':False,'message':'Unauthorized'}),flush=True)\n"
        " raise SystemExit(1)\n"
        "time.sleep(60)\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"a")
    (source / "b.bin").write_bytes(b"b")
    logger = logging.getLogger(f"parallel-process-{mode}-{uuid.uuid4()}")
    logger.addHandler(logging.NullHandler())
    progress = backup_module.Progress(time.monotonic(), 1, enabled=False)
    cancel = threading.Event()
    args = make_args(FakeRemote(), upload_workers=2)
    args.upload_timeout = 0.2 if mode == "timeout" else 10
    local = backup_module.scan_local_tree(source, progress, logger)
    runner = backup_module.CliRunner([sys.executable, str(script), mode], logger, progress, cancel)
    backup = backup_module.Backup(args=args, runner=runner, logger=logger, progress=progress, cancel_event=cancel,
                                  source_root=source, source_name=source.name, copy_contents=False, local_root=local)
    backup.upload_gate.observe(session_token(time.time(), time.time() + 86400))
    processes = []
    both_started = threading.Event()
    start_barrier = threading.Barrier(2)
    real_spawn = backup_module.subprocess.Popen
    def spawn(*args, **kwargs):
        process = real_spawn(*args, **kwargs)
        if args[0][0] == sys.executable:
            processes.append(process)
            if len(processes) == 2:
                both_started.set()
            start_barrier.wait(timeout=3)
        return process
    def cancel_workers():
        if both_started.wait(5):
            cancel.set()
    trigger = threading.Thread(target=cancel_workers) if mode == "cancel" else None
    if trigger:
        trigger.start()
    actions = [backup_module.FilePlan(file, "upload") for file in local.files]
    with mock.patch.object(backup_module.subprocess, "Popen", side_effect=spawn):
        if mode == "timeout":
            results = backup.run_upload_batch(actions, args.target)
            assert len(results) == 2 and all(r.timed_out and not r.success for r in results)
        else:
            expected = backup_module.CancelledError if mode == "cancel" else backup_module.AuthenticationError
            with pytest.raises(expected):
                backup.run_upload_batch(actions, args.target)
    if trigger:
        trigger.join(5)
        assert not trigger.is_alive()
    assert len(processes) == 2
    assert all(process.poll() is not None for process in processes)


def test_whoami_error_output_never_logs_private_login_material(tmp_path):
    script = tmp_path / "private_response.py"
    script.write_text("import sys\nprint('mnemonic=PRIVATE_WORDS privateKey=PRIVATE_KEY')\n"
                      "print('PRIVATE_STDERR',file=sys.stderr)\n", encoding="utf-8")
    log = io.StringIO()
    logger = logging.getLogger(f"private-login-{uuid.uuid4()}")
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(log))
    logger.setLevel(logging.INFO)
    runner = backup_module.CliRunner([sys.executable, str(script)], logger,
                                     backup_module.Progress(time.monotonic(), 1, enabled=False), threading.Event())
    result = runner.once(["whoami"], operation="whoami", path="token window", attempt=1, timeout=10)
    assert not result.success
    assert all(secret not in log.getvalue() + result.message + result.stderr
               for secret in ("PRIVATE_WORDS", "PRIVATE_KEY", "PRIVATE_STDERR"))


def test_unverified_new_cli_version_keeps_normal_backup_but_disables_parallelism(tmp_path):
    logger = logging.getLogger("new-cli-version-test")
    logger.addHandler(logging.NullHandler())
    seen = []
    session = backup_module.CommandResult(True, session_token(time.time(), time.time() + 86400), "", 0, 0)
    def process(backup):
        seen.append(backup.upload_gate.disabled)
        return 0
    with mock.patch.object(backup_module, "resolve_cli", return_value=(["fake"], "@internxt/cli/1.6.11 linux-x64")), \
         mock.patch.object(backup_module, "resolve_npm_prefix", return_value=None), \
         mock.patch.object(backup_module, "configure_logging", return_value=logger), \
         mock.patch.object(backup_module.subprocess, "run", return_value=backup_module.subprocess.CompletedProcess([], 0, "v22.18.0", "")), \
         mock.patch.object(backup_module.CliRunner, "once", return_value=session), \
         mock.patch.object(backup_module.Backup, "process", process):
        assert backup_module.main(["--source", str(tmp_path), "--target", new_uuid()]) == 0
    assert seen == [True]
    args = backup_module.build_parser().parse_args(["--source", str(tmp_path), "--target", new_uuid()])
    assert args.upload_workers == 2


def test_main_rejects_missing_source_before_cli_or_login(tmp_path):
    missing = tmp_path / "missing"
    with mock.patch.object(backup_module, "resolve_cli", side_effect=AssertionError("CLI should not be invoked")):
        with pytest.raises(SystemExit) as exit_info:
            backup_module.main(["--source", str(missing), "--target", ""])
    assert exit_info.value.code == 2
