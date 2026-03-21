import os
import json
import re
import shutil
import subprocess
import warnings
import platform
import uuid

import pytest
import sys

# Absolute path to the project root, so run_backup works regardless of where pytest is invoked
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# Fail early if internxt is not on PATH — the intended workflow is to run tests from a shell
# where 'internxt' is already available (e.g. after manually running 'internxt login-legacy').
if not shutil.which("internxt") and not shutil.which("internxt.cmd"):
    raise SystemExit(
        "ERROR: 'internxt' not found in PATH.\n"
        "Ensure the Internxt CLI is installed and your shell PATH includes it before running pytest."
    )


def _internxt_subprocess(args, **kwargs):
    """Run an internxt CLI command via subprocess.
    Uses shell=True on Windows for .cmd file resolution (same as internxt_backup.py lines 199-200).
    Uses subprocess.list2cmdline (not ' '.join) so arguments with spaces or shell
    metacharacters (e.g. passwords containing &, |, >, %) are correctly quoted."""
    cmd = ["internxt"] + [str(a) for a in args]
    if platform.system() == "Windows":
        return subprocess.run(subprocess.list2cmdline(cmd), shell=True, **kwargs)
    return subprocess.run(cmd, **kwargs)


def _internxt_login(credentials):
    """Log in via login-legacy (CLI v1.6.x). Does NOT use --json."""
    result = _internxt_subprocess(
        ["login-legacy", f"-e={credentials['email']}", f"-p={credentials['password']}", "-x"],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"internxt login-legacy failed (rc={result.returncode}): "
            f"{result.stdout!r} {result.stderr!r}"
        )


def _internxt_logout():
    """Log out. Best-effort — ignores all failures.
    Note: logout does NOT accept -x; called without it (matches script line 301)."""
    try:
        _internxt_subprocess(
            ["logout"],
            capture_output=True, text=True, timeout=30
        )
    except Exception:
        pass


def _is_logged_in():
    """Return True if the CLI currently has an active authenticated session."""
    try:
        result = _internxt_subprocess(
            ["whoami", "--json", "-x"],
            capture_output=True, text=True, timeout=15
        )
        out = json.loads(result.stdout)
        return bool(out.get("success"))
    except Exception:
        return False


def internxt(*args):
    """Run an internxt CLI command expecting JSON output. Raises on failure."""
    result = _internxt_subprocess(
        list(args) + ["--json", "-x"],
        capture_output=True, text=True, timeout=60
    )
    try:
        out = json.loads(result.stdout)
    except Exception:
        raise RuntimeError(
            f"internxt {args} returned non-JSON: {result.stdout!r} stderr: {result.stderr!r}"
        )
    if not out.get("success"):
        raise RuntimeError(f"internxt {args} failed: {out}")
    return out


def backup_cmd(src_dir, dest_id, credentials, extra_args=None):
    """Build the internxt_backup.py command list.
    Credentials are included only when available; if the CLI is already logged in
    the script ignores them anyway."""
    cmd = [
        sys.executable, "internxt_backup.py",
        "-s", src_dir,
        "-t", dest_id,
        "-r", "2",
        "--full-console-log",
    ]
    if credentials.get("email"):
        cmd += ["-e", credentials["email"]]
    if credentials.get("password"):
        cmd += ["-p", credentials["password"]]
    return cmd + (extra_args or [])


def run_backup(src_dir, dest_id, credentials, extra_args=None, expect_success=True):
    """Run internxt_backup.py as a blocking subprocess. Returns CompletedProcess.
    src_dir may be an absolute path or a path relative to PROJECT_ROOT."""
    result = subprocess.run(
        backup_cmd(src_dir, dest_id, credentials, extra_args),
        capture_output=True, text=True,
        timeout=300, cwd=PROJECT_ROOT
    )
    if expect_success and result.returncode != 0:
        raise AssertionError(
            f"Backup failed (rc={result.returncode}):\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def parse_summary(stdout):
    """Extract upload/skip/fail/remove counts from backup stdout via regex.
    Raises AssertionError if any expected summary line is missing (backup didn't complete)."""
    counts = {}
    for key, pattern in [
        ("uploaded", r"Files uploaded:\s+(\d+)"),
        ("skipped",  r"Files skipped:\s+(\d+)"),
        ("failed",   r"Files failed:\s+(\d+)"),
        ("removed",  r"Files removed:\s+(\d+)"),
    ]:
        m = re.search(pattern, stdout)
        assert m, f"Summary line '{key}' not found in output:\n{stdout}"
        counts[key] = int(m.group(1))
    return counts


def list_remote(folder_id):
    """Return the raw list dict for a remote folder: {"files": [...], "folders": [...]}."""
    out = internxt("list", f"--id={folder_id}")
    return out.get("list", {"files": [], "folders": []})


def remote_folder_names(folder_id):
    return remote_folder_names_from(list_remote(folder_id))


def remote_file_names(folder_id):
    return remote_file_names_from(list_remote(folder_id))


def _fix_encoding(text):
    """Mirror of internxt_backup.normalize_encoding: re-decode mojibake where
    UTF-8 bytes were mis-decoded as Latin-1 or Windows-1252."""
    for codec in ("latin1", "windows-1252"):
        try:
            fixed = text.encode(codec).decode("utf-8")
            if fixed != text:
                return fixed
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return text


def remote_folder_names_from(lst):
    """Extract folder plainNames from a pre-fetched list_remote() result."""
    return {_fix_encoding(f.get("plainName", f.get("name", ""))) for f in lst.get("folders", [])}


def remote_file_names_from(lst):
    """Reconstruct file name strings from a pre-fetched list_remote() result.
    Matches the key format used by internxt_backup.py: plainName + "." + type (or just plainName
    if type is empty, for extension-less files)."""
    result_set = set()
    for f in lst.get("files", []):
        pn = _fix_encoding(f.get("plainName", f.get("name", "")))
        t = (f.get("type") or "")
        result_set.add(f"{pn}.{t}" if t else pn)
    return result_set


@pytest.fixture(scope="session")
def credentials():
    email = os.environ.get("INTERNXT_EMAIL")       # optional if already logged in
    password = os.environ.get("INTERNXT_PASSWORD")  # optional if already logged in
    base_id = os.environ.get("INTERNXT_TEST_FOLDER_ID")
    if not base_id:
        pytest.skip("Set INTERNXT_TEST_FOLDER_ID to run integration tests")
    return {"email": email, "password": password, "base_id": base_id}


@pytest.fixture(scope="session")
def cli_session(credentials):
    """Ensure an authenticated CLI session exists for the duration of the test suite.

    Two modes:
    - Pre-authenticated: if 'internxt whoami' already succeeds (user logged in manually),
      the session is left untouched and no logout is performed on teardown.
    - Auto-login: if not logged in, INTERNXT_EMAIL and INTERNXT_PASSWORD must be set;
      the fixture logs in at the start and out at the end.

    In both cases each backup subprocess finds an existing session (whoami succeeds →
    logged_in = False in the script → no logout on exit), so the session persists
    across all tests without per-test re-authentication.
    """
    if _is_logged_in():
        yield   # leave the manually established session alone
        return
    if not credentials["email"] or not credentials["password"]:
        pytest.skip(
            "Not logged in and INTERNXT_EMAIL/INTERNXT_PASSWORD are not set. "
            "Either run 'internxt login-legacy' first or set both env vars."
        )
    _internxt_logout()   # clear any stale session from a previous run
    _internxt_login(credentials)
    yield
    _internxt_logout()


@pytest.fixture
def remote_folder(credentials, cli_session):
    """Create a fresh uniquely-named remote folder per test; delete it on teardown.

    delete-permanently-folder is confirmed to delete recursively (folder and all
    contents), so teardown works correctly regardless of how much was uploaded.
    """
    folder_name = f"pytest_{uuid.uuid4().hex[:8]}"
    out = internxt("create-folder", f"--id={credentials['base_id']}", "--name", folder_name)
    folder_uuid = out["folder"]["uuid"]
    assert folder_uuid, f"create-folder returned no UUID: {out}"
    yield folder_uuid
    try:
        internxt("delete-permanently-folder", f"--id={folder_uuid}")
    except Exception as e:
        warnings.warn(
            f"Teardown failed for remote folder {folder_uuid} ({folder_name}): {e}. "
            "Manual cleanup required."
        )
