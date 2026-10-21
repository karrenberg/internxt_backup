import shutil
import subprocess
import os
import platform
import signal
import time
from pathlib import Path

import pytest

from conftest import (
    backup_cmd,
    list_remote,
    parse_summary,
    remote_file_names,
    remote_file_names_from,
    remote_folder_names,
    remote_folder_names_from,
    run_backup,
    PROJECT_ROOT,
)


##############################################################################
# Simple atomic tests — one behaviour each
##############################################################################

@pytest.mark.integration
def test_upload_single_file(remote_folder, credentials, tmp_path):
    """A single file is uploaded to the correct remote location."""
    src = tmp_path / "single"
    src.mkdir()
    (src / "hello.txt").write_text("hello")

    run_backup(str(src), remote_folder, credentials)

    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"
    assert "hello.txt" in remote_file_names(root["uuid"]), "hello.txt not found remotely"


# Real uploads verify sync behavior with mixed file sizes; offline tests enforce worker scheduling deterministically.
@pytest.mark.integration
@pytest.mark.parametrize("no_prescan", [False, True])
def test_mixed_size_uploads_and_replacements(remote_folder, credentials, tmp_path, no_prescan):
    src = tmp_path / "parallel_uploads"
    src.mkdir()
    sizes = {"a.bin": 4096, "b.bin": 65536, "c.bin": 1024**2, "d.bin": 20 * 1024**2}
    pattern = bytes(range(256))
    for name, size in sizes.items():
        (src / name).write_bytes((pattern * (size // len(pattern) + 1))[:size])
    flags = ["--no-prescan"] if no_prescan else []
    first = run_backup(str(src), remote_folder, credentials, extra_args=flags)
    assert parse_summary(first.stdout)["uploaded"] == 4
    root = next(item for item in list_remote(remote_folder)["folders"] if item.get("plainName") == src.name)
    def observed_sizes():
        return {f"{item['plainName']}.{item['type']}" if item.get("type") else item["plainName"]:
                int(item["size"]) for item in list_remote(root["uuid"])["files"]}
    assert observed_sizes() == sizes
    rerun = run_backup(str(src), remote_folder, credentials, extra_args=flags)
    counts = parse_summary(rerun.stdout)
    assert counts["uploaded"] == 0 and counts["removed"] == 0 and counts["skipped"] == 4

    for name in ("a.bin", "b.bin"):
        with (src / name).open("ab") as output:
            output.write(b"!")
        sizes[name] += 1
    replaced = run_backup(str(src), remote_folder, credentials, extra_args=[*flags, "--allow-delete"])
    assert parse_summary(replaced.stdout)["uploaded"] == 2
    assert parse_summary(replaced.stdout)["removed"] == 0
    assert observed_sizes() == sizes

    (src / "c.bin").unlink()
    del sizes["c.bin"]
    cleaned = run_backup(str(src), remote_folder, credentials, extra_args=[*flags, "--allow-delete"])
    counts = parse_summary(cleaned.stdout)
    assert counts["uploaded"] == 0 and counts["removed"] == 1
    assert observed_sizes() == sizes


@pytest.mark.integration
def test_create_single_subfolder(remote_folder, credentials, tmp_path):
    """A subfolder is created remotely with the correct name."""
    src = tmp_path / "folder_test"
    src.mkdir()
    sub = src / "mysubfolder"
    sub.mkdir()
    (sub / "file.txt").write_text("content")

    run_backup(str(src), remote_folder, credentials)

    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"
    assert "mysubfolder" in remote_folder_names(root["uuid"]), "mysubfolder not found remotely"


@pytest.mark.integration
def test_renamed_folder_keeps_remote_uuid(remote_folder, credentials, tmp_path):
    """A uniquely matching local folder rename is applied remotely without re-uploading."""
    src = tmp_path / "rename_test"
    old_local = src / "old-name"
    old_local.mkdir(parents=True)
    (old_local / "same.txt").write_text("same")

    run_backup(str(src), remote_folder, credentials, extra_args=["--allow_delete", "--no-prescan"])
    root_listing = list_remote(remote_folder)
    root = next((f for f in root_listing["folders"] if f.get("plainName") == src.name), None)
    assert root is not None
    before = list_remote(root["uuid"])
    old_remote = next((f for f in before["folders"] if f.get("plainName") == "old-name"), None)
    assert old_remote is not None

    old_local.rename(src / "new-name")
    result = run_backup(str(src), remote_folder, credentials, extra_args=["--allow_delete", "--no-prescan"])

    after = list_remote(root["uuid"])
    new_remote = next((f for f in after["folders"] if f.get("plainName") == "new-name"), None)
    assert new_remote is not None
    assert new_remote["uuid"] == old_remote["uuid"]
    assert "old-name" not in remote_folder_names_from(after)
    counts = parse_summary(result.stdout)
    assert counts["uploaded"] == 0
    assert counts["removed"] == 0


@pytest.mark.integration
def test_skip_existing_file(remote_folder, credentials, tmp_path):
    """A file already uploaded is skipped (not re-uploaded) on the second run."""
    src = tmp_path / "skip_test"
    src.mkdir()
    (src / "file.txt").write_text("content")

    run_backup(str(src), remote_folder, credentials)
    result2 = run_backup(str(src), remote_folder, credentials)

    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, f"File was re-uploaded:\n{result2.stdout}"
    assert counts["skipped"] == 1, f"Expected 1 skipped, got {counts['skipped']}:\n{result2.stdout}"


@pytest.mark.integration
def test_delete_single_file(remote_folder, credentials, tmp_path):
    """A locally-deleted file is preserved remotely without --allow_delete,
    and removed remotely once --allow_delete is set."""
    src = tmp_path / "del_test"
    src.mkdir()
    (src / "gone.txt").write_text("gone")

    run_backup(str(src), remote_folder, credentials)
    (src / "gone.txt").unlink()

    # Without the flag: file must be preserved
    run_backup(str(src), remote_folder, credentials)
    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"
    assert "gone.txt" in remote_file_names(root["uuid"]), \
        "gone.txt was deleted without --allow_delete"

    # With the flag: file must be removed
    run_backup(str(src), remote_folder, credentials, extra_args=["--allow_delete"])
    assert "gone.txt" not in remote_file_names(root["uuid"]), \
        "gone.txt was not deleted with --allow_delete"


@pytest.mark.integration
def test_internxtignore_simple(remote_folder, credentials, tmp_path):
    """A folder containing .internxtignore is excluded; a sibling folder is uploaded."""
    src = tmp_path / "ignore_test"
    src.mkdir()
    ignored = src / "ignored"
    ignored.mkdir()
    (ignored / ".internxtignore").write_text("")
    (ignored / "secret.txt").write_text("secret")
    kept = src / "kept"
    kept.mkdir()
    (kept / "visible.txt").write_text("visible")

    run_backup(str(src), remote_folder, credentials)

    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"
    folder_names = remote_folder_names(root["uuid"])
    assert "kept" in folder_names, f"'kept' folder missing: {folder_names}"
    assert "ignored" not in folder_names, f"'ignored' folder uploaded despite .internxtignore: {folder_names}"


@pytest.mark.integration
def test_internxtignore_added_after_upload(remote_folder, credentials, tmp_path):
    """A folder that was previously uploaded is removed remotely when a .internxtignore
    is added to it locally and --allow_delete is set on the next run."""
    src = tmp_path / "ignore_added_test"
    src.mkdir()
    sub = src / "will_be_ignored"
    sub.mkdir()
    (sub / "file.txt").write_text("content")

    # First run: folder is uploaded normally
    run_backup(str(src), remote_folder, credentials)
    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"
    assert "will_be_ignored" in remote_folder_names(root["uuid"]), \
        "will_be_ignored not uploaded on first run"

    # Add .internxtignore — folder is now locally ignored
    (sub / ".internxtignore").write_text("")

    # Without --allow_delete: folder must remain remotely
    run_backup(str(src), remote_folder, credentials)
    assert "will_be_ignored" in remote_folder_names(root["uuid"]), \
        "will_be_ignored was removed without --allow_delete"

    # With --allow_delete: folder must be removed remotely
    run_backup(str(src), remote_folder, credentials, extra_args=["--allow_delete"])
    assert "will_be_ignored" not in remote_folder_names(root["uuid"]), \
        "will_be_ignored was not removed despite .internxtignore and --allow_delete"


##############################################################################
# Compound / regression tests
##############################################################################

@pytest.mark.integration
def test_folder_names_exact(remote_folder, credentials):
    """Remote folder names must be exactly what was requested — no mangling of any kind."""
    run_backup("tests/2019", remote_folder, credentials)

    top_listing = list_remote(remote_folder)
    top_names = remote_folder_names_from(top_listing)
    assert top_names == {"2019"}, f"Expected exactly {{'2019'}}, got: {top_names}"

    folder_2019 = next((f for f in top_listing["folders"] if f.get("plainName") == "2019"), None)
    assert folder_2019 is not None

    sub_names = remote_folder_names(folder_2019["uuid"])
    # tests/2019 has a/, b/, Käfer/ — should_be_ignored2/ is excluded by .internxtignore
    assert sub_names == {"a", "b", "Käfer"}, \
        f"Expected exactly {{'a', 'b', 'Käfer'}}, got: {sub_names}"


@pytest.mark.integration
def test_idempotency(remote_folder, credentials):
    """Second run must upload nothing — all files already present at correct size (Bug 1)."""
    run_backup("tests/2019", remote_folder, credentials)
    result2 = run_backup("tests/2019", remote_folder, credentials)
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, f"Second run uploaded {counts['uploaded']} files:\n{result2.stdout}"
    assert counts["failed"] == 0, f"Second run had failures:\n{result2.stdout}"
    # Guard against vacuous pass: the second run must have seen files to skip
    assert counts["skipped"] >= 1, f"Second run skipped 0 files — first run may have failed silently:\n{result2.stdout}"


@pytest.mark.integration
def test_extension_less_file_recognised(remote_folder, credentials, tmp_path):
    """Extension-less files must be recognised on second run, not re-uploaded (Bug 7).
    Bug 7: key is built as plainName + "." + type; when type="" the key becomes "Makefile."
    which never matches local "Makefile", causing infinite re-upload."""
    src = tmp_path / "ext_test"
    src.mkdir()
    (src / "Makefile").write_text("all: build")
    (src / "README").write_text("readme content")
    (src / "normal.txt").write_text("normal")

    run_backup(str(src), remote_folder, credentials)
    result2 = run_backup(str(src), remote_folder, credentials)
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, f"Extension-less files re-uploaded on second run:\n{result2.stdout}"


@pytest.mark.integration
def test_internxtignore_respected(remote_folder, credentials):
    """Folders containing .internxtignore must not be uploaded — both at top level and nested.

    Fixture structure (verified):
    - tests/2019/b/should_be_ignored/   has .internxtignore INSIDE the folder
    - tests/2019/should_be_ignored2/    has .internxtignore directly under 2019/
    """
    run_backup("tests/2019", remote_folder, credentials)

    lst = list_remote(remote_folder)
    folder_2019 = next((f for f in lst["folders"] if f.get("plainName") == "2019"), None)
    assert folder_2019 is not None, \
        f"Folder '2019' not found; got: {[f.get('plainName') for f in lst['folders']]}"

    contents_2019 = list_remote(folder_2019["uuid"])
    top_folder_names = {f.get("plainName", f.get("name", "")) for f in contents_2019.get("folders", [])}

    # Expected content IS present (guards against an empty listing giving a false pass)
    assert "a" in top_folder_names, f"'a' missing from 2019: {top_folder_names}"
    assert "b" in top_folder_names, f"'b' missing from 2019: {top_folder_names}"

    # should_be_ignored2 is directly under 2019 — must NOT appear
    assert "should_be_ignored2" not in top_folder_names, \
        f"should_be_ignored2 uploaded despite .internxtignore: {top_folder_names}"

    # should_be_ignored is under b/ — find b_folder and verify
    b_folder = next((f for f in contents_2019.get("folders", []) if f.get("plainName") == "b"), None)
    assert b_folder is not None, \
        f"Folder 'b' not found inside 2019; got: {list(top_folder_names)}"
    b_contents = remote_folder_names(b_folder["uuid"])
    assert "c" in b_contents, f"'c' missing from b: {b_contents}"
    assert "should_be_ignored" not in b_contents, \
        f"should_be_ignored uploaded despite .internxtignore: {b_contents}"


@pytest.mark.integration
def test_unicode_filenames(remote_folder, credentials):
    """Unicode filenames and folder names must upload and be idempotent."""
    run_backup("tests/2019", remote_folder, credentials)

    lst = list_remote(remote_folder)
    folder_2019 = next((f for f in lst["folders"] if f.get("plainName") == "2019"), None)
    assert folder_2019 is not None, \
        f"Folder '2019' not found; got: {[f.get('plainName') for f in lst['folders']]}"

    listing_2019 = list_remote(folder_2019["uuid"])
    file_names = remote_file_names_from(listing_2019)
    assert any("Özge" in n for n in file_names), f"Özge file not found: {file_names}"

    folder_names = remote_folder_names_from(listing_2019)
    assert "Käfer" in folder_names, f"Käfer folder not found: {folder_names}"

    result2 = run_backup("tests/2019", remote_folder, credentials)
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, f"Unicode files re-uploaded on second run:\n{result2.stdout}"


@pytest.mark.integration
def test_allow_delete(remote_folder, credentials, tmp_path):
    """Remote-only files must be deleted when --allow_delete is set."""
    src = tmp_path / "delete_test"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "delete_me.txt").write_text("delete")
    run_backup(str(src), remote_folder, credentials)

    (src / "delete_me.txt").unlink()
    run_backup(str(src), remote_folder, credentials, extra_args=["--allow_delete"])

    lst = list_remote(remote_folder)
    root_folder = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root_folder is not None, f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"
    file_names = remote_file_names(root_folder["uuid"])
    assert "keep.txt" in file_names, f"keep.txt missing: {file_names}"
    assert "delete_me.txt" not in file_names, f"delete_me.txt not deleted: {file_names}"


@pytest.mark.integration
def test_no_delete_without_flag(remote_folder, credentials, tmp_path):
    """Remote files must be preserved when --allow_delete is NOT set (negative test)."""
    src = tmp_path / "nodelete_test"
    src.mkdir()
    (src / "keep.txt").write_text("keep")
    (src / "stay.txt").write_text("stay")
    run_backup(str(src), remote_folder, credentials)

    (src / "stay.txt").unlink()
    run_backup(str(src), remote_folder, credentials)   # no --allow_delete

    lst = list_remote(remote_folder)
    root_folder = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root_folder is not None, f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"
    file_names = remote_file_names(root_folder["uuid"])
    assert "stay.txt" in file_names, \
        f"stay.txt was deleted even without --allow_delete: {file_names}"


@pytest.mark.integration
def test_nested_structure(remote_folder, credentials):
    """Files must land in the correct deeply-nested remote folder, not be misplaced to root (Bug 1).
    Bug 1: folder UUID tracking broken — all files fall back to DEST_ROOT_ID."""
    run_backup("tests/2019/b", remote_folder, credentials)

    def find_subfolder(parent_id, name):
        lst = list_remote(parent_id)
        match = next((f for f in lst["folders"] if f.get("plainName") == name), None)
        assert match is not None, \
            f"Subfolder '{name}' not found; got: {[f.get('plainName') for f in lst['folders']]}"
        return match["uuid"]

    b_id = find_subfolder(remote_folder, "b")

    # b/should_be_ignored/ has .internxtignore — must not have been uploaded
    b_folder_names = remote_folder_names(b_id)
    assert "should_be_ignored" not in b_folder_names, \
        f"should_be_ignored uploaded despite .internxtignore: {b_folder_names}"

    c_id = find_subfolder(b_id, "c")
    d_id = find_subfolder(c_id, "d")

    # 04.txt must be inside d/, not misplaced to root (the Bug 1 symptom)
    d_files = remote_file_names(d_id)
    assert "04.txt" in d_files, f"04.txt not found in d/: {d_files}"

    root_files = remote_file_names(remote_folder)
    assert "04.txt" not in root_files, f"04.txt misplaced to root (Bug 1 active): {root_files}"


@pytest.mark.integration
def test_special_char_filenames(remote_folder, credentials):
    """Files with special characters in names (& etc.) must upload and be idempotent."""
    run_backup("tests/2019", remote_folder, credentials)

    lst = list_remote(remote_folder)
    folder_2019 = next((f for f in lst["folders"] if f.get("plainName") == "2019"), None)
    assert folder_2019 is not None, \
        f"Folder '2019' not found; got: {[f.get('plainName') for f in lst['folders']]}"

    file_names = remote_file_names(folder_2019["uuid"])
    assert "bad_char_abc&def.txt" in file_names, \
        f"bad_char_abc&def.txt not found: {file_names}"

    result2 = run_backup("tests/2019", remote_folder, credentials)
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, f"Special-char files re-uploaded on second run:\n{result2.stdout}"


@pytest.mark.integration
def test_literal_percent_and_folder_suffix_names(remote_folder, credentials, tmp_path):
    """Names that resemble URL escapes or folder markers stay literal through sync."""
    src = tmp_path / "literal_names"
    src.mkdir()
    expected = {
        "literal%20name.txt": b"percent escape is literal",
        "100% done.txt": b"percent sign is literal",
        "file.folder": b"extension is literal",
        "README": b"extensionless name is literal",
        "snow-\u96ea-\u00d6.txt": b"unicode name is literal",
    }
    for name, contents in expected.items():
        (src / name).write_bytes(contents)

    run_backup(str(src), remote_folder, credentials)
    root_listing = list_remote(remote_folder)
    root = next(item for item in root_listing["folders"] if item.get("plainName") == src.name)
    listing = list_remote(root["uuid"])
    observed = {f"{item['plainName']}.{item['type']}" if item.get("type") else item["plainName"]:
                int(item["size"]) for item in listing["files"]}
    assert observed == {name: len(contents) for name, contents in expected.items()}

    rerun = run_backup(str(src), remote_folder, credentials)
    counts = parse_summary(rerun.stdout)
    assert counts["uploaded"] == 0
    assert counts["skipped"] == len(expected)


@pytest.mark.integration
def test_allow_delete_folder(remote_folder, credentials, tmp_path):
    """Remote-only folders must be deleted when --allow_delete is set."""
    src = tmp_path / "folder_delete_test"
    src.mkdir()
    keep_sub = src / "keep_folder"
    keep_sub.mkdir()
    (keep_sub / "file.txt").write_text("content")
    del_sub = src / "delete_folder"
    del_sub.mkdir()
    (del_sub / "file2.txt").write_text("content2")

    run_backup(str(src), remote_folder, credentials)

    shutil.rmtree(str(del_sub))

    run_backup(str(src), remote_folder, credentials, extra_args=["--allow_delete"])

    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"
    subfolder_names = remote_folder_names(root["uuid"])
    assert "keep_folder" in subfolder_names, f"keep_folder missing: {subfolder_names}"
    assert "delete_folder" not in subfolder_names, \
        f"delete_folder not deleted with --allow_delete: {subfolder_names}"


@pytest.mark.integration
def test_size_mismatch_no_overwrite(remote_folder, credentials, tmp_path):
    """A locally-modified file (different size) must NOT be re-uploaded without --allow_delete.
    The script must log a size-mismatch warning."""
    src = tmp_path / "mismatch_test"
    src.mkdir()
    f = src / "test.txt"
    f.write_text("original")

    run_backup(str(src), remote_folder, credentials)

    f.write_text("modified content that is longer than original")

    result2 = run_backup(str(src), remote_folder, credentials)
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, \
        f"File re-uploaded without --allow_delete:\n{result2.stdout}"
    assert "Keeping different-size remote file" in result2.stderr and "--allow-delete" in result2.stderr, \
        f"Expected size-mismatch warning in stderr:\n{result2.stderr}"

    # The remote file must still be present — not silently deleted
    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, f"Root folder '{src.name}' missing after second run"
    assert "test.txt" in remote_file_names(root["uuid"]), \
        "Remote file was unexpectedly deleted during second run"


@pytest.mark.integration
def test_size_mismatch_with_overwrite(remote_folder, credentials, tmp_path):
    """A locally-modified file (different size) must be re-uploaded when --allow_delete is set."""
    src = tmp_path / "overwrite_test"
    src.mkdir()
    f = src / "test.txt"
    f.write_text("original")

    run_backup(str(src), remote_folder, credentials)

    f.write_text("modified content that is longer than original")

    result2 = run_backup(str(src), remote_folder, credentials, extra_args=["--allow_delete"])
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 1, \
        f"Modified file was not re-uploaded with --allow_delete:\n{result2.stdout}"

    # Verify the file is in the correct remote location (not misplaced by Bug 1)
    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, \
        f"Root folder '{src.name}' not found after overwrite: {remote_folder_names_from(lst)}"
    file_names = remote_file_names(root["uuid"])
    assert "test.txt" in file_names, \
        f"Re-uploaded test.txt not found in correct remote folder: {file_names}"


@pytest.mark.integration
def test_spaces_in_path(remote_folder, credentials, tmp_path):
    """Files and folders with spaces in names must upload correctly and be idempotent.

    Bug 9: ' '.join(cmd) with shell=True on Windows splits on spaces, breaking any file
    path or folder name that contains a space. Fix: subprocess.list2cmdline(cmd)."""
    src = tmp_path / "folder with spaces"
    src.mkdir()
    (src / "file with spaces.txt").write_text("content")
    sub = src / "sub folder"
    sub.mkdir()
    (sub / "nested file.txt").write_text("nested")

    run_backup(str(src), remote_folder, credentials)

    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, \
        f"Root folder '{src.name}' not found: {remote_folder_names_from(lst)}"

    root_listing = list_remote(root["uuid"])
    file_names = remote_file_names_from(root_listing)
    assert "file with spaces.txt" in file_names, \
        f"File with spaces missing from remote: {file_names}"
    folder_names = remote_folder_names_from(root_listing)
    assert "sub folder" in folder_names, \
        f"Subfolder with spaces missing from remote: {folder_names}"

    # Second run must upload 0 (idempotency with spaces in paths)
    result2 = run_backup(str(src), remote_folder, credentials)
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, \
        f"Files with spaces re-uploaded on second run:\n{result2.stdout}"


@pytest.mark.integration
def test_interrupted_backup(remote_folder, credentials, tmp_path):
    """A backup interrupted mid-run must resume correctly: already-uploaded files are
    skipped and only the remaining or incomplete files are repaired on the next run.

    Wait for a confirmed upload in the backup log, then send a handled interrupt so
    the wrapper can terminate and reap its owned CLI process tree.
    """
    src = tmp_path / "interrupt_test"
    src.mkdir()
    file_size = 5 * 1024**2
    for i in range(8):
        content = f"content {i}".encode("utf-8")
        (src / f"file{i:02d}.bin").write_bytes((content * (file_size // len(content) + 1))[:file_size])

    started_at = time.time()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if platform.system() == "Windows" else 0
    proc = subprocess.Popen(
        backup_cmd(str(src), remote_folder, credentials),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=PROJECT_ROOT,
        shell=False,
        creationflags=creationflags,
        start_new_session=platform.system() != "Windows",
    )

    def send_interrupt():
        if proc.poll() is not None:
            return
        if platform.system() == "Windows":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)

    def kill_owned_tree():
        if proc.poll() is not None:
            return
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=10, check=False, shell=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)

    # Use the flushed file log instead of a special backup-only progress flag.
    deadline = time.time() + 120
    upload_seen = False
    while time.time() < deadline:
        log_files = [path for path in Path(PROJECT_ROOT).glob("backup_*.log")
                     if path.stat().st_mtime >= started_at]
        if log_files:
            latest_log = max(log_files, key=lambda path: path.stat().st_mtime_ns)
            text = latest_log.read_text(encoding="utf-8")
            upload_seen = any("CLI result: upload-file" in line and "success=True" in line
                              for line in text.splitlines())
        if upload_seen:
            break
        time.sleep(0.05)
    else:
        send_interrupt()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            kill_owned_tree()
        pytest.fail("No uploads detected within 120s — backup may be stuck or all files skipped")

    try:
        send_interrupt()
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        kill_owned_tree()
        pytest.fail("Interrupted backup did not stop its owned process tree within 30 seconds")
    assert proc.returncode == 130, f"Expected interrupt exit status 130, got {proc.returncode}"

    # Ground truth: query remote directly to see what was actually uploaded
    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, "Root folder not created — no uploads completed before kill"
    files_after_interrupt = remote_file_names(root["uuid"])
    n_done = len(files_after_interrupt)
    assert n_done >= 1, f"Expected ≥1 file uploaded before interrupt: {files_after_interrupt}"
    if n_done >= 8:
        pytest.skip(
            "All files uploaded before the interrupt took effect; the recovery rerun is unnecessary."
        )

    # A partial file is repaired only with the explicit replacement/deletion policy.
    result2 = run_backup(str(src), remote_folder, credentials, extra_args=["--allow-delete"])
    counts = parse_summary(result2.stdout)
    assert counts["failed"] == 0, f"Second run had failures:\n{result2.stdout}"
    assert counts["skipped"] + counts["uploaded"] == 8, \
        f"Expected all 8 files skipped or uploaded, got {counts}:\n{result2.stdout}"

    # Every file must be present at the expected size after recovery.
    all_remote = remote_file_names(root["uuid"])
    for i in range(8):
        assert f"file{i:02d}.bin" in all_remote, \
            f"file{i:02d}.bin missing after second run: {all_remote}"
    listing = list_remote(root["uuid"])
    sizes = {f"{item.get('plainName', '')}.{item.get('type')}" if item.get("type") else item.get("plainName", ""):
             int(item["size"]) for item in listing["files"]}
    assert all(sizes.get(f"file{i:02d}.bin") == file_size for i in range(8))


@pytest.mark.integration
def test_prescan_count_nested(remote_folder, credentials, tmp_path):
    """Pre-scan must report 0 files to upload on a second run against an already-backed-up
    nested structure.

    Regression test: the pre-scan used a hardcoded '/' separator in rel_cur_dir while
    normalize_rel_path uses os.path.join (backslashes on Windows), causing a key mismatch
    that made every file inside nested folders appear as needing upload even when already
    present remotely."""
    src = tmp_path / "prescan_nested"
    src.mkdir()
    (src / "root.txt").write_text("root")
    deep = src / "sub1" / "sub2"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("deep")

    run_backup(str(src), remote_folder, credentials)
    result2 = run_backup(str(src), remote_folder, credentials)

    assert "Pre-scan complete: 0 file(s)" in result2.stdout, \
        f"Pre-scan over-counted on second run (path separator bug?):\n{result2.stdout}"
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, f"Files re-uploaded on second run:\n{result2.stdout}"
