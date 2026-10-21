"""Integration-only helpers; importing this module performs no CLI or network work."""

import os
import subprocess
import sys
import uuid
import warnings

import pytest

import internxt_backup


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _internxt_subprocess(args, **kwargs):
    """Invoke the resolved Node entry point directly, with no shell expansion."""
    command, _version = internxt_backup.resolve_cli()
    return subprocess.run(command + [str(arg) for arg in args], shell=False, **kwargs)


def internxt(*args):
    result = _internxt_subprocess(list(args) + ["--json", "-x"], capture_output=True, text=True,
                                  encoding="utf-8", errors="strict", timeout=300)
    output = internxt_backup.parse_cli_output(result.stdout)
    if result.returncode != 0 or not isinstance(output, dict) or output.get("success") is not True:
        # Session responses may contain private keys or recovery material even when the command fails.
        detail = ("Session check failed; log in manually with `internxt login`." if args[0] == "whoami" else
                  internxt_backup.error_message(output, result.stdout, result.stderr, "Missing CLI success response"))
        raise RuntimeError(f"Internxt CLI {args[0]!r} failed: {detail}")
    return output


def list_remote(folder_id):
    response = internxt("list", f"--id={folder_id}")
    internxt_backup.validate_listing(response)
    return response["list"]


def remote_folder_names(folder_id):
    return remote_folder_names_from(list_remote(folder_id))


def remote_file_names(folder_id):
    return remote_file_names_from(list_remote(folder_id))


def remote_folder_names_from(listing):
    return {item.get("plainName", "") for item in listing.get("folders", [])}


def remote_file_names_from(listing):
    names = set()
    for item in listing.get("files", []):
        name = item.get("plainName", "")
        extension = item.get("type") or ""
        names.add(f"{name}.{extension}" if extension else name)
    return names


def backup_cmd(src_dir, dest_id, credentials=None, extra_args=None):
    # Credentials remain an ignored compatibility parameter so existing tests cannot pass secrets to the script.
    return [
        sys.executable,
        "internxt_backup.py",
        "-s", str(src_dir),
        "-t", str(dest_id),
        "-r", "2",
        "--full-console-log",
        *(extra_args or []),
    ]


def run_backup(src_dir, dest_id, credentials=None, extra_args=None, expect_success=True):
    result = subprocess.run(
        backup_cmd(src_dir, dest_id, credentials, extra_args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=3600,
        cwd=PROJECT_ROOT,
        shell=False,
    )
    if expect_success and result.returncode != 0:
        raise AssertionError(f"Backup failed (rc={result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return result


def parse_summary(stdout):
    import re

    patterns = {
        "uploaded": r"Files uploaded:\s+(\d+)",
        "skipped": r"Files skipped \(same size\):\s+(\d+)",
        "failed": r"Files deleted:.*?failed:\s+(\d+)",
        "removed": r"Files deleted:\s+(\d+)",
    }
    counts = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, stdout)
        assert match, f"Summary line {key!r} missing from output:\n{stdout}"
        counts[key] = int(match.group(1))
    return counts


@pytest.fixture(scope="session")
def credentials():
    # Live tests require an explicit disposable parent; no account-specific defaults are published.
    configured = os.environ.get("INTERNXT_TEST_FOLDER_ID")
    if not configured:
        pytest.skip("Set INTERNXT_TEST_FOLDER_ID to a disposable test parent folder to enable integration tests.")
    try:
        internxt_backup.validate_uuid(configured, "Integration test parent folder UUID")
    except internxt_backup.BackupError as exc:
        pytest.fail(str(exc))
    return {"base_id": configured}


@pytest.fixture(scope="session")
def cli_session(credentials):
    try:
        response = internxt("whoami")
    except Exception as exc:
        pytest.skip(f"Log in manually with `internxt login` before integration tests: {exc}")
    if response.get("success") is not True:
        pytest.skip("Log in manually with `internxt login` before integration tests.")
    # This fixture deliberately leaves the existing CLI session untouched.
    yield


@pytest.fixture
def remote_folder(credentials, cli_session):
    """Create and remove one unique direct child of the explicitly configured test parent."""
    folder_name = f"backup-test-{uuid.uuid4().hex}"
    parent_uuid = credentials["base_id"]
    try:
        out = internxt("create-folder", f"--id={parent_uuid}", "--name", folder_name)
        folder_uuid = out.get("folder", {}).get("uuid")
        if not isinstance(folder_uuid, str) or not folder_uuid:
            raise RuntimeError(f"create-folder returned no UUID for {folder_name!r}.")
    except Exception as creation_error:
        try:
            listing = internxt_backup.validate_listing(internxt("list", f"--id={parent_uuid}"))
            matches = [item for item in listing.folders.values() if item.name == folder_name]
        except Exception as inspection_error:
            raise RuntimeError(f"Could not reconcile the unique test folder {folder_name!r}.") from inspection_error
        if len(matches) != 1:
            raise RuntimeError(f"Could not reconcile the unique test folder {folder_name!r}.") from creation_error
        folder_uuid = matches[0].uuid
    yield folder_uuid
    try:
        internxt("delete-permanently-folder", f"--id={folder_uuid}")
    except Exception as exc:
        try:
            listing = internxt_backup.validate_listing(internxt("list", f"--id={parent_uuid}"))
            if all(item.uuid != folder_uuid for item in listing.folders.values()):
                return
        except Exception:
            pass
        warnings.warn(f"Test folder cleanup failed for owned UUID {folder_uuid}: {exc}")
