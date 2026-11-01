"""Exercise copy semantics against the existing scripted remote, without a login or network."""

import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import internxt_client
import internxtcp
from test_backup_unit import FakeRemote


# Reuse the backup suite's remote model so both commands face the same uncertain CLI outcomes.
@pytest.fixture
def client():
    remote = FakeRemote()
    args = internxtcp.build_parser().parse_args(["unused", "-t", "/", "--max-attempts", "3"])
    logger = logging.getLogger("internxtcp-test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    progress = internxt_client.Progress(time.monotonic(), None, enabled=False)
    copier = internxtcp.Copy(args=args, runner=remote, logger=logger, progress=progress,
                            cancel_event=threading.Event(), allow_overwrite=True)

    def immediate_retry(*_args):
        copier.stats.retry_count += 1

    copier.retry_wait = immediate_retry
    return copier, remote


def inventory(path, copier):
    if path.is_dir():
        return internxt_client.scan_local_tree(path, copier.progress, copier.logger, honor_ignore=False)
    stat = path.stat()
    return internxt_client.LocalFile(path, path.name, path.name, stat.st_size, stat.st_mtime_ns)


# Placement and overwrite assertions capture the differences between copying and backup policy.
def test_single_file_uses_absolute_named_target_and_ignores_local_siblings(tmp_path, client):
    copier, remote = client
    first = remote.add_folder("", "Photos & Käfer")
    target = remote.add_folder(first.uuid, "2026")
    source = tmp_path / "Özge & photo.txt"
    source.write_bytes(b"photo")
    (tmp_path / "unrelated.txt").write_bytes(b"leave alone")
    assert copier.process(inventory(source, copier), [first.name, target.name]) == 0
    assert set(remote.directories[target.uuid].files) == {source.name}
    uploads = [call[0] for call in remote.calls if call[0][0] == "upload-file"]
    assert uploads == [["upload-file", "--file", str(source), f"--destination={target.uuid}"]]


@pytest.mark.parametrize("old_size", [4, 100])
def test_existing_files_are_overwritten_even_at_equal_size(tmp_path, client, old_size):
    copier, remote = client
    source = tmp_path / "same.txt"
    source.write_bytes(b"new!")
    old = remote.add_file("", source.name, old_size)
    retained = remote.add_file("", "remote-only.txt", 20)
    assert copier.process(inventory(source, copier), []) == 0
    assert remote.directories[""].files[source.name].uuid != old.uuid
    assert remote.directories[""].files[retained.name].uuid == retained.uuid
    assert "--overwrite" in next(call[0] for call in remote.calls if call[0][0] == "upload-file")
    assert copier.stats.files_replaced == 1


def test_recursive_copy_merges_named_directory_and_includes_ignore_markers(tmp_path, client):
    copier, remote = client
    target = remote.add_folder("", "destination")
    source = tmp_path / "mydir"
    nested = source / "Käfer"
    nested.mkdir(parents=True)
    (nested / ".internxtignore").write_bytes(b"")
    (nested / "file.txt").write_bytes(b"contents")
    (source / "empty").mkdir()
    existing = remote.add_folder(target.uuid, source.name)
    retained = remote.add_folder(existing.uuid, "remote-only")
    remote.add_file(existing.uuid, "keep.txt", 1)
    assert copier.process(inventory(source, copier), [target.name]) == 0
    root_listing = remote.directories[existing.uuid]
    assert root_listing.folders[retained.name].uuid == retained.uuid
    assert "keep.txt" in root_listing.files
    nested_listing = remote.directories[root_listing.folders[nested.name].uuid]
    assert set(nested_listing.files) == {".internxtignore", "file.txt"}
    assert not remote.directories[root_listing.folders["empty"].uuid].all_items()
    assert not any(call[0][0].startswith(("delete", "rename")) for call in remote.calls)


@pytest.mark.parametrize("blocked", [False, True])
def test_missing_or_file_target_fails_before_mutation(tmp_path, client, blocked):
    copier, remote = client
    if blocked:
        remote.add_file("", "target", 1)
    source = tmp_path / "file"
    source.write_bytes(b"x")
    with pytest.raises(internxt_client.BranchError, match="Target folder"):
        copier.process(inventory(source, copier), ["target"])
    assert all(call[0][0] == "list" for call in remote.calls)


@pytest.mark.parametrize("directory", [False, True])
def test_file_directory_collisions_are_errors(tmp_path, client, directory):
    copier, remote = client
    source = tmp_path / "collision"
    if directory:
        source.mkdir()
        remote.add_file("", source.name, 1)
    else:
        source.write_bytes(b"x")
        remote.add_folder("", source.name)
    assert copier.process(inventory(source, copier), []) == 1
    assert not any(call[0][0] in ("upload-file", "create-folder") for call in remote.calls)


def test_uuid_looking_names_and_internxt_are_literal_folders(client):
    copier, remote = client
    first = remote.add_folder("", "internxt")
    target = remote.add_folder(first.uuid, remote.root_uuid)
    assert copier.resolve_target([first.name, target.name]) == target.uuid
    assert [call[0][1] for call in remote.calls] == ["--id=", f"--id={first.uuid}"]


# Uncertain writes must reconcile or retry rather than turning stale metadata into success.
def test_lost_new_upload_response_reconciles_without_reupload(tmp_path, client):
    copier, remote = client
    source = tmp_path / "file"
    source.write_bytes(b"contents")
    remote.next_upload = lambda model, *_args: model.result(None, False, "502 Bad Gateway")
    assert copier.process(inventory(source, copier), []) == 0
    assert copier.stats.files_reconciled == 1
    assert len([call for call in remote.calls if call[0][0] == "upload-file"]) == 1


def test_lost_folder_response_reuses_reconciled_folder(tmp_path, client):
    copier, remote = client
    source = tmp_path / "directory"
    source.mkdir()
    (source / "file").write_bytes(b"x")
    remote.next_create = lambda model, *_args: model.result(None, False, "502 Bad Gateway")
    assert copier.process(inventory(source, copier), []) == 0
    assert len([call for call in remote.calls if call[0][0] == "create-folder"]) == 1
    folder = remote.directories[""].folders[source.name]
    assert "file" in remote.directories[folder.uuid].files


@pytest.mark.parametrize("always_fails", [False, True])
def test_uncertain_equal_size_overwrite_requires_confirmed_upload(tmp_path, client, always_fails):
    copier, remote = client
    source = tmp_path / "file"
    source.write_bytes(b"new!")
    original = remote.add_file("", source.name, 4)
    real_once = remote.once
    attempted_uploads = []

    def once(args, **kwargs):
        if args[0] == "upload-file":
            attempted_uploads.append(args)
            if always_fails or len(attempted_uploads) == 1:
                remote.calls.append((list(args), kwargs["operation"], kwargs["path"],
                                     kwargs["attempt"], kwargs["timeout"]))
                return remote.result(None, False, "502 Bad Gateway")
        return real_once(args, **kwargs)

    remote.once = once
    assert copier.process(inventory(source, copier), []) == (1 if always_fails else 0)
    assert len(attempted_uploads) == (3 if always_fails else 2)
    assert copier.stats.files_reconciled == 0
    if always_fails:
        assert remote.directories[""].files[source.name].uuid == original.uuid
        assert copier.stats.files_uploaded == 0


def test_transient_listing_retries_and_permanent_error_reports_failure(tmp_path, client):
    copier, remote = client
    source = tmp_path / "file"
    source.write_bytes(b"x")
    remote.next_list = lambda model, *_args: model.result(None, False, "502 Bad Gateway")
    remote.next_upload = lambda model, *_args: model.result(None, False, "Permission denied")
    assert copier.process(inventory(source, copier), []) == 1
    assert copier.stats.retry_count == 1
    assert copier.stats.files_uploaded == 0


def test_source_change_during_upload_is_incomplete(tmp_path, client):
    copier, remote = client
    source = tmp_path / "file"
    source.write_bytes(b"original")

    def mutate(_model, _parent, path, _item, result):
        path.write_bytes(b"changed after scan")
        return result

    remote.next_upload = mutate
    assert copier.process(inventory(source, copier), []) == 1
    assert "changed" in copier.stats.failures[0]


def test_size_limit_failure_does_not_block_independent_zero_byte_file(tmp_path, client, monkeypatch):
    copier, remote = client
    source = tmp_path / "directory"
    source.mkdir()
    (source / "large").write_bytes(b"too large")
    (source / "zero").write_bytes(b"")
    monkeypatch.setattr(internxtcp, "FILE_SIZE_UPLOAD_LIMIT_BYTES", 1)
    assert copier.process(inventory(source, copier), []) == 1
    root = remote.directories[""].folders[source.name]
    assert set(remote.directories[root.uuid].files) == {"zero"}
    assert copier.stats.files_uploaded == 1 and copier.stats.files_too_large == 1


def test_termination_after_final_upload_cannot_report_success(tmp_path, client):
    copier, remote = client
    source = tmp_path / "file"
    source.write_bytes(b"x")

    def terminate(_model, _parent, _path, _item, result):
        copier.cancel_event.set()
        return result

    remote.next_upload = terminate
    with pytest.raises(internxt_client.CancelledError):
        copier.process(inventory(source, copier), [])


# CLI validation and entry points must work before any CLI installation, login, or logging.
@pytest.mark.parametrize("target, expected", [("/", []), ("/Photos/2026/", ["Photos", "2026"]),
                                             ("//Photos//", ["Photos"])])
def test_absolute_target_components(target, expected):
    assert internxtcp.target_parts(target) == expected


@pytest.mark.parametrize("arguments", [
    ["missing", "-t", "/"], [".", "-t", "/"], ["-r", ".", "-t", "relative/path"],
    ["-r", ".", "-t", "/a/../b"], ["-r", ".", "-t", "/a/./b"],
    ["-r", ".", "-t", "/", "--max-attempts", "0"],
    ["-r", ".", "-t", "/", "--upload-timeout", "nan"],
])
def test_invalid_input_never_invokes_cli(arguments, monkeypatch):
    def unexpected():
        raise AssertionError("CLI must not run for invalid input")
    monkeypatch.setattr(internxtcp, "resolve_cli", unexpected)
    with pytest.raises(SystemExit) as result:
        internxtcp.main(arguments)
    assert result.value.code == 2


def test_help_and_import_are_independent_of_backup():
    root = Path(__file__).resolve().parents[1]
    code = "import sys, internxtcp; assert 'internxt_backup' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True, capture_output=True, timeout=10)
    for entry in ("internxtcp.py", "internxtcp"):
        result = subprocess.run([sys.executable, str(root / entry), "--help"],
                                cwd=root, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and "--recursive" in result.stdout
    if sys.platform == "win32":
        result = subprocess.run(["cmd.exe", "/d", "/c", str(root / "internxtcp.cmd"), "--help"],
                                cwd=root, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0 and "--recursive" in result.stdout


@pytest.mark.parametrize("interrupt", [False, True])
def test_main_runs_file_copy_and_restores_signal_handlers(tmp_path, client, monkeypatch, interrupt):
    copier, remote = client
    source = tmp_path / "file"
    source.write_bytes(b"x")
    monkeypatch.setattr(internxtcp, "resolve_cli", lambda: (["fake"], "@internxt/cli/1.6.10"))
    monkeypatch.setattr(internxtcp, "configure_logging", lambda *_args, **_kwargs: copier.logger)
    monkeypatch.setattr(internxtcp, "CliRunner", lambda *_args: remote)
    if interrupt:
        def cancel(*_args):
            raise internxt_client.CancelledError("cancelled")
        monkeypatch.setattr(internxtcp.Copy, "process", cancel)
    previous = signal.getsignal(signal.SIGINT)
    assert internxtcp.main([str(source), "-t", "/"]) == (130 if interrupt else 0)
    assert signal.getsignal(signal.SIGINT) == previous
    if not interrupt:
        assert source.name in remote.directories[""].files
