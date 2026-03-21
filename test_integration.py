import shutil
import subprocess
import time

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
    assert "different size" in result2.stdout and "--allow-delete" in result2.stdout, \
        f"Expected size-mismatch message in output:\n{result2.stdout}"

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
    skipped and only the remaining files are uploaded on the next run.

    Uses --progress-file to detect upload completions without parsing noisy stdout.
    Validates remote state via list_remote() (same ground-truth approach as all other tests).
    """
    src = tmp_path / "interrupt_test"
    src.mkdir()
    # Files must be large enough that uploads take several hundred ms each so that the
    # process can be reliably killed after the first file but before the last.
    for i in range(6):
        (src / f"file{i:02d}.txt").write_text(f"content {i}" * 50_000)

    progress_file = tmp_path / "progress.txt"
    # Use DEVNULL to prevent stdout/stderr pipe buffer deadlock: with --full-console-log
    # the script can produce enough output to fill the OS pipe buffer (64 KB on Windows),
    # blocking the process before any uploads complete.
    proc = subprocess.Popen(
        backup_cmd(str(src), remote_folder, credentials,
                   extra_args=["--progress-file", str(progress_file)]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=PROJECT_ROOT,
    )

    # Wait until at least 1 file is confirmed uploaded, then kill
    deadline = time.time() + 120
    while time.time() < deadline:
        if progress_file.exists() and progress_file.read_text().strip():
            break
        time.sleep(0.05)
    else:
        proc.kill()
        proc.wait()
        pytest.fail("No uploads detected within 120s — backup may be stuck or all files skipped")

    proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Ground truth: query remote directly to see what was actually uploaded
    lst = list_remote(remote_folder)
    root = next((f for f in lst["folders"] if f.get("plainName") == src.name), None)
    assert root is not None, "Root folder not created — no uploads completed before kill"
    files_after_interrupt = remote_file_names(root["uuid"])
    n_done = len(files_after_interrupt)
    assert n_done >= 1, f"Expected ≥1 file uploaded before kill: {files_after_interrupt}"
    if n_done >= 6:
        pytest.skip(
            "All 6 files uploaded before kill — kill was too late. "
            "Enlarge file content or add more files to get a reliable interruption window."
        )

    # Second run: already-uploaded files skipped, remainder uploaded
    result2 = run_backup(str(src), remote_folder, credentials)
    counts = parse_summary(result2.stdout)
    assert counts["failed"] == 0, f"Second run had failures:\n{result2.stdout}"
    assert counts["skipped"] == n_done, \
        f"Expected {n_done} skipped (already uploaded), got {counts['skipped']}:\n{result2.stdout}"
    assert counts["uploaded"] == 6 - n_done, \
        f"Expected {6 - n_done} remaining files uploaded, got {counts['uploaded']}:\n{result2.stdout}"

    # All 6 files must be present remotely after the second run
    all_remote = remote_file_names(root["uuid"])
    for i in range(6):
        assert f"file{i:02d}.txt" in all_remote, \
            f"file{i:02d}.txt missing after second run: {all_remote}"


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

    assert "0 files" in result2.stdout, \
        f"Pre-scan over-counted on second run (path separator bug?):\n{result2.stdout}"
    counts = parse_summary(result2.stdout)
    assert counts["uploaded"] == 0, f"Files re-uploaded on second run:\n{result2.stdout}"
