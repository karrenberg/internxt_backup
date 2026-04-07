#!/usr/bin/env python3

import os
import subprocess
import json
import sys
import logging
import time
import argparse
import platform
import signal
import atexit
import getpass
from urllib.parse import unquote

# TODO: Validate that all files have been uploaded using "list"
# TODO: This is written against @internxt/cli/1.5.4 win32-x64 node-v22.18.0, validate version
# TODO: Validate sufficient remote space ("config" lists available / used space)

start_time = time.time()

INTERNXT_CLI_BINARY = r"internxt"
IGNOREFILE_NAME = ".internxtignore"
FILE_SIZE_UPLOAD_LIMIT_BYTES = 21474836480

# Git bash has problems with the password input.
if 'MSYSTEM' in os.environ and os.environ['MSYSTEM'].startswith(('MINGW', 'MSYS')):
    print("Warning: Secure password input may not work in Git Bash. If it hangs, use cmd or PowerShell.")

################################################################################
# Argument parsing
################################################################################

# Parse arguments
# TODO: Take "DST_DIR" arg (relative path string), create "find_id" helper, convert path to DEST_ROOT_ID
parser = argparse.ArgumentParser(description="Backup uploader script.")
parser.add_argument("-s", "--source", dest="src_dir", required=True, help="Source directory to upload")
parser.add_argument("-t", "--target", dest="dest_id", required=True, nargs='?', const='', default=None, help="Destination base folder UUID (empty string or omit value for drive root)")
parser.add_argument("-v", "--verbose", dest="verbose_mode", action='store_true', help="Enable verbose logging")
parser.add_argument("-l", "--full-console-log", dest="full_console_log", action='store_true', help="log everything that is logged to file to the console as well")
parser.add_argument("-r", "--max_num_retries", dest="max_num_retries", required=False, default=5, type=int, help="Set the maximum number of retries for internxt CLI commands (default: 5)")
parser.add_argument("-w", "--retry_wait_seconds", dest="retry_wait_seconds", required=False, default=3, type=int, help="Set N, where N^{retry attempt} is the number of seconds to wait before the next retry (default: 3)")
parser.add_argument("-d", "--allow_delete", dest="allow_delete", action='store_true', help="Delete remote files/folders if they do not exist locally or are ignored")
parser.add_argument("-e", "--email", dest="email", required=False, help="Email for Internxt login")
parser.add_argument("-p", "--password", dest="password", required=False, help="Password for Internxt login (not recommended to use on CLI)")
parser.add_argument("--progress-file", dest="progress_file", required=False, default=None, help="Append one line per uploaded file to this path (used by tests to detect upload progress)")
parser.add_argument("--no-prescan", dest="no_prescan", action='store_true', help="Skip pre-scan; show honest metrics without percentage and ETA (pre-scan is on by default)")
args = parser.parse_args()

MAX_NUM_RETRIES = args.max_num_retries
RETRY_SLEEP_BASE_SECONDS = args.retry_wait_seconds # 2 = wait for 2, 4, 8, 16, 32 seconds; 3 = 3, 9, 27, 81, 243 seconds ; 4 = wait for 4, 16, 64, 256, 1024 seconds

################################################################################
# Logging
################################################################################

# Set up logging: always print everything to file, suppress stdout/stderr output if requested

# If this is False (verbose = True), everything is also printed to the console
ENABLE_SUPPRESS = not args.full_console_log

# This can be set to True to temporarily suppress all stdout/stderr (except if ENABLE_SUPPRESS is False)
SUPPRESS_STDOUT_STDERR = False

class FlushStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

class StdoutFilter(logging.Filter):
    def filter(self, record):
        # If a record explicitly opts out of console, or general suppression is active, drop it from stdout
        if getattr(record, 'suppress_console', False):
            return False
        if SUPPRESS_STDOUT_STDERR and ENABLE_SUPPRESS:
            return False
        # Suppress ERROR and all higher levels on stdout.
        if record.levelno >= logging.ERROR:
            return False
        return True

class StderrFilter(logging.Filter):
    def filter(self, record):
        # If a record explicitly opts out of console, or general suppression is active, drop it from stderr
        if getattr(record, 'suppress_console', False):
            return False
        if SUPPRESS_STDOUT_STDERR and ENABLE_SUPPRESS:
            return False
        # Suppress all levels lower than ERROR on stderr.
        if record.levelno < logging.ERROR:
            return False
        return True

logfile_name = f"backup_{time.strftime('%Y%m%d_%H%M%S')}.log"
file_handler = logging.FileHandler(logfile_name, mode='w', encoding='utf-8')
file_handler.setLevel(logging.DEBUG if args.verbose_mode else logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

# Logging handler for info level -> stdout
logging_handler_info = FlushStreamHandler(sys.stdout)
logging_handler_info.setLevel(logging.DEBUG if args.verbose_mode else logging.INFO)
logging_handler_info.setFormatter(logging.Formatter('%(message)s'))
logging_handler_info.addFilter(StdoutFilter())

# Logging handler for error level -> stderr
logging_handler_error = FlushStreamHandler()
logging_handler_error.setLevel(logging.ERROR)
logging_handler_error.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logging_handler_error.addFilter(StderrFilter())

logging.basicConfig(level=logging.DEBUG if args.verbose_mode else logging.INFO, handlers=[file_handler, logging_handler_info, logging_handler_error])

################################################################################
# Helpers
################################################################################

def format_size(num):
    for unit in ['B','KB','MB','GB','TB']:
        if abs(num) < 1024.0:
            return "%3.1f%s" % (num, unit)
        num /= 1024.0
    return "%3.1f%s" % (num, 'PB')

def format_hhmmss(seconds):
    """Format seconds as HH:MM:SS, allowing hours > 24."""
    if seconds is None:
        return "00:00:00"
    total = int(seconds) if seconds > 0 else 0
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

# Prevent names such as ".\\mydir", return "mydir" instead
def normalize_rel_path(parent, name):
    return name if parent == "." else os.path.join(parent, name)

def normalize_encoding(text):
    # Try treating the input as Latin1 encoded UTF-8
    try:
        text_fixed = text.encode("latin1").decode("utf8")
        if text_fixed != text:
            logging.debug(f"Encoding changed (latin1 to UTF-8): {text} -> {text_fixed}", extra={'suppress_console': ENABLE_SUPPRESS})
        return text_fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass  # Not Latin1 pretending to be UTF-8

    # Try treating the input as Windows-1252 encoded UTF-8
    try:
        text_fixed = text.encode("windows-1252").decode("utf-8")
        if text_fixed != text:
            logging.debug(f"Encoding changed (Windows-1252 to UTF-8): {text} -> {text_fixed}", extra={'suppress_console': ENABLE_SUPPRESS})
        return text_fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    return text  # Last resort, return as-is

################################################################################
# Internxt CLI
################################################################################

def sanitize_command_for_logging(cmd):
    """Sanitize command arguments for logging to remove sensitive information."""
    if not cmd or len(cmd) < 2:
        return cmd

    # Check if this is a login command
    if cmd[1] not in ("login", "login-legacy"):
        return cmd

    # Create a copy to avoid modifying the original
    sanitized_cmd = cmd.copy()

    # Look for password arguments and mask them
    for i, arg in enumerate(sanitized_cmd):
        if arg.startswith("-p=") or arg.startswith("--password="):
            # Mask the password part
            if "=" in arg:
                prefix, _ = arg.split("=", 1)
                sanitized_cmd[i] = f"{prefix}=<hidden>"
            else:
                sanitized_cmd[i] = "<hidden>"

    return sanitized_cmd

def run_cli(args, force_interactive=False, stop_on_message=None, override_num_retries=None, suppress_console_errors=False):
    """Run the CLI with args and return parsed JSON output."""
    cmd = [INTERNXT_CLI_BINARY] + args + ["--json"] + ([] if force_interactive else ["-x"])

    # Determine the number of retries.
    cur_max_num_retries = override_num_retries if override_num_retries is not None else MAX_NUM_RETRIES

    # Retry logic for transient failures
    for attempt in range(1, cur_max_num_retries + 1):
        sanitized_cmd = sanitize_command_for_logging(cmd)
        logging.debug(f"Running command (attempt {attempt}): {' '.join(sanitized_cmd)}", extra={'suppress_console': suppress_console_errors})

        # Attempt the command
        # Use shell=True on Windows to get proper command resolution (e.g., internxt -> internxt.cmd)
        if platform.system() == "Windows":
            # shell=True is required to resolve .cmd files (e.g. internxt.cmd).
            # Wrapping every arg in double-quotes makes & | < > ^ literal to cmd.exe.
            # Known limitation: %VAR% inside arg values is still expanded by cmd.exe;
            # filenames or passwords containing %WORD% patterns may be silently mangled.
            cmd_str = ' '.join(f'"{a}"' for a in cmd)
            result = subprocess.run(cmd_str, shell=True, capture_output=True, text=True)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True)

        num_retries = attempt - 1

        try:
            out = json.loads(result.stdout)
        except Exception:
            logging.error(f"Command failed (exception during JSON parsing) (attempt {attempt}): {' '.join(sanitized_cmd)}", extra={'suppress_console': suppress_console_errors})
            logging.error(result.stderr, extra={'suppress_console': suppress_console_errors})
            logging.error(result.stdout, extra={'suppress_console': suppress_console_errors})
            if attempt < cur_max_num_retries:
                time.sleep(RETRY_SLEEP_BASE_SECONDS ** attempt)
                continue
            return None, num_retries, False

        if not isinstance(out, dict):
            logging.warning(f"Command returned non-dict JSON (attempt {attempt}): {result.stdout[:200]}", extra={'suppress_console': suppress_console_errors})
            logging.error(f"Command failed (invalid JSON) (attempt {attempt}): {' '.join(sanitized_cmd)}", extra={'suppress_console': suppress_console_errors})
            logging.error(result.stderr, extra={'suppress_console': suppress_console_errors})
            logging.error(result.stdout, extra={'suppress_console': suppress_console_errors})
            assert(result.returncode != 0)
            if attempt < cur_max_num_retries:
                time.sleep(RETRY_SLEEP_BASE_SECONDS ** attempt)
                continue
            return None, num_retries, False

        if out.get("success") is not True:
            msg = out.get("message")
            if stop_on_message is not None and msg is not None and stop_on_message in msg:
                return None, num_retries, True
            logging.error(f"Command failed (attempt {attempt}): {' '.join(sanitized_cmd)}", extra={'suppress_console': suppress_console_errors})
            logging.error(f"Message: {msg}", extra={'suppress_console': suppress_console_errors})
            if attempt < cur_max_num_retries:
                time.sleep(RETRY_SLEEP_BASE_SECONDS ** attempt)
                continue
            return None, num_retries, False

        if result.returncode != 0:
            logging.error(f"Command failed with returncode != 0 (attempt {attempt}): {' '.join(sanitized_cmd)}", extra={'suppress_console': suppress_console_errors})
            logging.error(json.dumps(out, indent=2), extra={'suppress_console': suppress_console_errors})
            if attempt < cur_max_num_retries:
                time.sleep(RETRY_SLEEP_BASE_SECONDS ** attempt)
                continue
            return None, num_retries, False

        # Success!
        return out, num_retries, False

    # max_num_retries=0 -> loop body never executes; fall through here.
    return None, 0, False

################################################################################
# Log in
################################################################################

# Check if we're logged in.
result, num_retries, stopped_on_message = run_cli(["whoami"], stop_on_message="You are not logged in")

# If something else went wrong, bail out.
if result is None and not stopped_on_message:
    logging.error(f"whoami failed")
    sys.exit(1)

logged_in = False
if stopped_on_message:
    # Not logged in, check for credentials
    if not args.email:
        logging.error("Not logged in and no email provided. Please provide --email (and optionally --password) to log in.")
        sys.exit(1)
    logging.info("Not logged in.")
    email = args.email
    password = args.password
    if password is None:
        # Prompt for password securely
        logging.info("Requesting password...")
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            password = getpass.getpass(prompt=f"Password for {email}: ")
        except Exception as e:
            logging.error(f"Error reading password: {e}")
            sys.exit(1)
    if not password:
        logging.error("No password provided. Exiting.")
        sys.exit(1)
    logging.info("Attempting login...")
    result, num_retries, _ = run_cli(["login-legacy", f"-e={email}", f"-p={password}"])
    if result is None:
        logging.error(f"login failed")
        sys.exit(1)
    # Remember that we logged in automatically so we attempt to log out upon exit.
    logging.info("Login successful")
    logged_in = True

################################################################################
# Graceful shutdown handler
################################################################################

def graceful_shutdown():
    """Helper function to handle graceful shutdown, including logout if logged in."""
    global logged_in
    # 'logged_in' in globals() guard: sys.exit(1) can fire before logged_in is assigned.
    if 'logged_in' not in globals() or not logged_in:
        logging.info("Shutting down (no logout needed)")
        return
    logged_in = False   # set False BEFORE logout so a second call is a no-op even if logout raises
    logging.info("Attempting to log out from Internxt...")
    try:
        # Logout doesn't have -x so we must run it in "interactive" mode
        logout_result, _, _ = run_cli(["logout"], override_num_retries=3, force_interactive=True)
        if logout_result is not None:
            logging.info("Successfully logged out from Internxt")
        else:
            logging.warning("Failed to log out from Internxt")
    except Exception as e:
        logging.warning(f"Exception during logout: {e}")

# Register the graceful shutdown function to be called on normal exit
atexit.register(graceful_shutdown)

# Set up signal handler for graceful shutdown
def signal_handler(signum, frame):
    """Handle system signals for graceful shutdown."""
    print(f"\nReceived signal {signum}, shutting down...", flush=True)
    sys.exit(1)   # triggers atexit -> graceful_shutdown

# Register signal handlers for common termination signals
signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
if platform.system() != "Windows":
    signal.signal(signal.SIGHUP, signal_handler)  # Hangup signal (Unix only)

################################################################################
# Remote Folder Helpers & UUID Cache
################################################################################

# Cache for directory listings
remote_dir_cache = {}

def list_remote_directory(folder_uuid):
    """List contents of remote directory, returns dict of {name: metadata}."""
    result, num_retries, _ = run_cli(["list", f"--id={folder_uuid}"])

    if result is None:
        logging.error(f"list failed, exiting")
        sys.exit(1)

    if num_retries > 0:
        logging.info(f"List command required {num_retries} retries: list --id={folder_uuid}")

    items = {}
    for item in result.get("list", {}).get("folders", []):
        if "plainName" not in item:
            # Encrypted/opaque name: can never match a local folder name; skip rather than
            # storing under item["name"] which would cause false deletions with --allow-delete.
            logging.warning(f"Remote folder missing 'plainName' (uuid={item.get('uuid')}), skipping")
            continue
        # Use decrypted name (plainName) if available.
        # Make sure we convert the result to UTF-8, otherwise file name matching is broken.
        # See files loop below for the unquote() rationale and known edge case.
        items[normalize_encoding(unquote(item["plainName"]))] = item

    for item in result.get("list", {}).get("files", []):
        if "plainName" not in item:
            logging.warning(f"Remote file missing 'plainName' (uuid={item.get('uuid')}), skipping")
            continue
        ext = item.get("type") or ""   # API returns null (not "") for extension-less files
        # Use decrypted name (plainName) if available.
        # Make sure we convert the result to UTF-8, otherwise file name matching is broken.
        # unquote() decodes percent-encoded UTF-8 sequences the API embeds in plainName
        # (e.g. Käfer -> K%C3%A4fer -> Käfer). Known edge case: a file literally named
        # "my%20file.txt" would be decoded to "my file.txt" and fail to match locally.
        # This is accepted; non-ASCII filenames failing is far more common.
        name = normalize_encoding(unquote(item["plainName"]))
        items[f"{name}.{ext}" if ext else name] = item

    return items

def get_cached_dir_listing(folder_uuid):
    """Get directory listing, using cache if available."""
    if folder_uuid not in remote_dir_cache:
        remote_dir_cache[folder_uuid] = list_remote_directory(folder_uuid)
    return remote_dir_cache[folder_uuid]

def get_or_create_folder(parent_items, parent_uuid, folder_name, parent_rel):
    """Get existing folder UUID or create new folder and return its UUID."""
    # Check if folder exists in parent_items
    assert(parent_items != None)
    existing = parent_items.get(folder_name)
    if existing and existing.get("type") == "folder":
        folder_uuid = existing.get("uuid")
        logging.info(f"Found existing folder '{folder_name}' in '{parent_rel}' -> ID: {folder_uuid}", extra={'suppress_console': ENABLE_SUPPRESS})
        return folder_uuid

    # Create new folder if it doesn't exist
    out, num_retries, _ = run_cli(["create-folder", f"--id={parent_uuid}", "--name", folder_name])

    if out is None:
        logging.error(f"create-folder failed, exiting")
        sys.exit(1)

    if num_retries > 0:
        logging.info(f"Create-folder command required {num_retries} retries: create-folder --id={parent_uuid} --name {folder_name!r}")

    folder_obj = out.get("folder")
    folder_uuid = folder_obj.get("uuid") if folder_obj else None
    if not folder_uuid:
        logging.error(f"Failed to create folder '{folder_name}'. Output:")
        logging.error(json.dumps(out, indent=2))
        sys.exit(1)

    # Only log this to file, don't spam stdout.
    logging.info(f"Created folder '{folder_name}' in '{parent_rel}' -> ID: {folder_uuid}", extra={'suppress_console': ENABLE_SUPPRESS})

    # Invalidate parent's cache since we modified it
    remote_dir_cache.pop(parent_uuid, None)
    return folder_uuid

def get_or_create_folder_from_uuid(parent_uuid, folder_name, parent_rel):
    items = get_cached_dir_listing(parent_uuid)
    return get_or_create_folder(items, parent_uuid, folder_name, parent_rel)

################################################################################
# Source/target directory setup
################################################################################

SRC_DIR = normalize_encoding(args.src_dir)
DEST_ROOT_ID = args.dest_id

# Mapping from relative path to destination folder UUID
folder_uuids = {}
folder_uuids["."] = DEST_ROOT_ID

# Ensure the base source folder itself exists remotely (so we can nest into it)
src_name = os.path.basename(os.path.normpath(SRC_DIR))
DEST_ROOT_ID = get_or_create_folder_from_uuid(DEST_ROOT_ID, normalize_encoding(src_name), ".")

# re-map "." to the newly created/validated root folder UUID
folder_uuids["."] = DEST_ROOT_ID

################################################################################
# Phase 1: Quick local scan (no network) - compute totals for progress display
################################################################################

total_local_size = 0
total_folder_count = 0
total_skipped_size = 0
total_skipped_files = 0

for cur_dir, dirs, files in os.walk(SRC_DIR):
    cur_dir = normalize_encoding(cur_dir)
    # Check for .internxtignore file and skip traversal
    if IGNOREFILE_NAME in files:
        if cur_dir == SRC_DIR:
            logging.warning(f"Source directory itself contains {IGNOREFILE_NAME} - this is likely a misconfiguration. The ignore file will be ignored for the source root.")
        else:
            dirs.clear()  # prevents walking into subdirectories
            continue
    total_folder_count += 1
    for f in files:
        abs_path = os.path.join(cur_dir, f)
        try:
            sz = os.path.getsize(abs_path)
        except Exception as e:
            logging.error(f"Could not determine size of file {abs_path}: {e}")
            continue
        # Skip files that exceed the upload limit
        if sz <= FILE_SIZE_UPLOAD_LIMIT_BYTES:
            total_local_size += sz
        else:
            total_skipped_size += sz
            total_skipped_files += 1

logging.info(f"Local scan: {total_folder_count} folder(s), {format_size(total_local_size)} total.")
if total_skipped_size > 0:
    logging.info(f"  Skipped files that exceed upload limit ({format_size(FILE_SIZE_UPLOAD_LIMIT_BYTES)}): {total_skipped_files} file(s), {format_size(total_skipped_size)} total.")

################################################################################
# Progress line helper
################################################################################

_last_line_len = 0

def print_line(msg):
    """Write a single updating status line to stdout, padding to overwrite previous content."""
    global _last_line_len
    # Clear the line if the next message is shorter than the previous one.
    # This is done by moving the cursor back to the start of the line
    # and writing spaces to overwrite the previous output.
    padded = msg + ' ' * max(0, _last_line_len - len(msg))
    # Print the output
    sys.stdout.write('\r' + padded)
    sys.stdout.flush()
    _last_line_len = len(msg)

################################################################################
# Delete helpers
################################################################################

removed_folders = []
removed_files = []
removed_size = 0

def delete_remote_folder(rel_path, folder_uuid):
    # TODO: Calculate the size of the folder. This can be an expensive operation so we don't do it for now.
    # folder_size = calculate_remote_folder_size(rel_path, folder_uuid)

    # Delete the folder.
    out, _, _ = run_cli(["delete-permanently-folder", f"--id={folder_uuid}"], suppress_console_errors=ENABLE_SUPPRESS)
    if out is None:
        logging.error(f"Failed to delete folder '{rel_path}'", extra={'suppress_console': ENABLE_SUPPRESS})
    else:
        # Update stats after deletion.
        removed_folders.append(rel_path)
        # Remove folder from cache.
        remote_dir_cache.pop(folder_uuid, None)

def delete_remote_file(rel_path, file_uuid, file_size):
    global removed_size
    out, _, _ = run_cli(["delete-permanently-file", f"--id={file_uuid}"], suppress_console_errors=ENABLE_SUPPRESS)
    if out is None:
        logging.error(f"Failed to delete file '{rel_path}'", extra={'suppress_console': ENABLE_SUPPRESS})
    else:
        # Update stats after deletion.
        removed_files.append(rel_path)
        removed_size += file_size

################################################################################
# Optional pre-scan: walk remote to compute exact upload size for progress bar
################################################################################

to_upload_size = None  # None = no prescan; int = exact bytes to upload
to_upload_count = None  # None = no prescan; int = number of files to upload

if not args.no_prescan:
    logging.info("Pre-scanning remote to calculate upload size...")
    prescan_start = time.time()

    # prescan_uuids mirrors folder_uuids but is never written to for non-existent folders.
    # None means the folder does not exist remotely; all files inside need uploading.
    prescan_uuids = {".": DEST_ROOT_ID}
    to_upload_size = 0
    to_upload_count = 0

    for cur_dir, dirs, files in os.walk(SRC_DIR):
        cur_dir = normalize_encoding(cur_dir)
        rel_cur_dir = normalize_encoding(os.path.relpath(cur_dir, SRC_DIR))

        # Same .internxtignore skip logic as Phase 1
        if IGNOREFILE_NAME in files:
            if cur_dir != SRC_DIR:
                dirs.clear()
                continue

        remote_uuid = prescan_uuids.get(rel_cur_dir)

        if remote_uuid is None:
            # Folder absent remotely: every file in it (and all descendants) needs uploading
            for f in files:
                abs_path = os.path.join(cur_dir, f)
                try:
                    sz = os.path.getsize(abs_path)
                except OSError:
                    continue
                if sz <= FILE_SIZE_UPLOAD_LIMIT_BYTES:
                    to_upload_size += sz
                    to_upload_count += 1
            # Propagate None to all subdirectories
            for d in dirs:
                d_name = normalize_encoding(d)
                prescan_uuids[normalize_rel_path(rel_cur_dir, d_name)] = None
            continue

        # Folder exists remotely: check its listing
        remote_items = get_cached_dir_listing(remote_uuid)

        # Propagate child folder UUIDs (or None if absent)
        for d in dirs[:]:
            d_name = normalize_encoding(d)
            if os.path.isfile(os.path.join(cur_dir, d, IGNOREFILE_NAME)):
                dirs.remove(d)
                continue
            item = remote_items.get(d_name)
            child_uuid = item["uuid"] if (item and item.get("type") == "folder") else None
            prescan_uuids[normalize_rel_path(rel_cur_dir, d_name)] = child_uuid

        # Determine which files need uploading
        for f in files:
            f_name = normalize_encoding(f)
            abs_path = os.path.join(cur_dir, f)
            try:
                file_size = os.path.getsize(abs_path)
            except OSError:
                continue
            if file_size > FILE_SIZE_UPLOAD_LIMIT_BYTES:
                continue
            remote_item = remote_items.get(f_name)
            if remote_item is None:
                to_upload_size += file_size
                to_upload_count += 1
            else:
                try:
                    remote_size = int(remote_item.get("size", 0))
                except Exception:
                    to_upload_size += file_size  # unreadable size: assume needs upload
                    to_upload_count += 1
                    continue
                if remote_size != file_size and args.allow_delete:
                    to_upload_size += file_size  # will be deleted and re-uploaded
                    to_upload_count += 1

    prescan_elapsed = time.time() - prescan_start
    logging.info(f"Pre-scan completed in {format_hhmmss(prescan_elapsed)}: {to_upload_count} files ({format_size(to_upload_size)}) to upload.")

################################################################################
# Phase 2: os.walk combined check+upload
#
# Key correctness property: folder_uuids[rel_cur_dir] is always set before
# os.walk visits a directory - root is pre-set above, every subdirectory is
# pre-set by its parent's Step 4 before os.walk descends into it.
################################################################################

num_created_folders = 0
uploaded_files = []
skipped_files = []
# Retry stats
num_failed_files = 0
num_retried_files = 0
num_total_retries = 0
uploaded_size = 0
skipped_size = 0
processed_size = 0
elapsed_upload_time = 0.0
last_file_mbps = 0  # MB/s of the most recently uploaded file; None until first upload
# For per-folder stats
folder_upload_stats = {}
folder_local_size = {}   # rel_cur_dir -> total local file size (within upload limit)
folder_remote_size = {}  # rel_cur_dir -> total remote file size before upload
processed_folders = 0

upload_start_time = time.time()
logging.info(f"Starting backup: {total_folder_count} folder(s).")

for cur_dir, dirs, files in os.walk(SRC_DIR):
    cur_dir = normalize_encoding(cur_dir)
    rel_cur_dir = normalize_encoding(os.path.relpath(cur_dir, SRC_DIR))

    # Step 1: get UUID for this directory.
    # Direct access - KeyError here means a bug in Step 4 of the parent iteration.
    folder_uuid = folder_uuids[rel_cur_dir]

    # Step 2: progress line.
    processed_folders += 1
    print_line(f"Checking [{processed_folders}/{total_folder_count}]: {rel_cur_dir}")

    # Step 3: fetch remote listing for this directory.
    remote_items = get_cached_dir_listing(folder_uuid)
    # Compute total remote file size for this folder (files only, not subfolders).
    _remote_sz = 0
    for _r_item in remote_items.values():
        if _r_item.get("type") != "folder":
            try:
                _remote_sz += int(_r_item.get("size", 0))
            except Exception:
                pass
    folder_remote_size[rel_cur_dir] = _remote_sz

    # Step 4: pre-create/find all immediate subdirectories; prune .internxtignore folders.
    # Iterating dirs[:] (a copy) because we mutate dirs to control os.walk's descent.
    # Track items deleted here so Step 5 doesn't attempt a second deletion.
    ignored_and_deleted = set()
    for d in dirs[:]:
        d_name = normalize_encoding(d)
        if os.path.isfile(os.path.join(cur_dir, d, IGNOREFILE_NAME)):
            # Folder d contains .internxtignore - treat as ignored.
            logging.info(f"Skipping folder with {IGNOREFILE_NAME}: {normalize_rel_path(rel_cur_dir, d_name)}", extra={'suppress_console': ENABLE_SUPPRESS})
            if args.allow_delete:
                existing = remote_items.get(d_name)
                if existing and existing.get("type") == "folder":
                    logging.info(f"Deleting remote ignored folder: {normalize_rel_path(rel_cur_dir, d_name)}", extra={'suppress_console': ENABLE_SUPPRESS})
                    delete_remote_folder(normalize_rel_path(rel_cur_dir, d_name), existing["uuid"])
                    ignored_and_deleted.add(d_name)
            dirs.remove(d)   # prevents os.walk from descending into d
            continue

        # Get existing remote folder or create a new one; record UUID for Step 1 of the child visit.
        was_existing = d_name in remote_items and remote_items[d_name].get("type") == "folder"
        d_uuid = get_or_create_folder(remote_items, folder_uuid, d_name, rel_cur_dir)
        if not was_existing:
            num_created_folders += 1
        folder_uuids[normalize_rel_path(rel_cur_dir, d_name)] = d_uuid

    # Step 5: delete remote-only items (only when --allow-delete).
    if args.allow_delete:
        local_dir_names  = {normalize_encoding(d) for d in dirs}
        local_file_keys  = {normalize_encoding(f) for f in files}

        for r_name, r_item in list(remote_items.items()):
            if r_name in ignored_and_deleted:
                continue   # already deleted in Step 4
            r_uuid = r_item.get("uuid")
            if not r_uuid:
                logging.warning(f"Remote item '{normalize_rel_path(rel_cur_dir, r_name)}' has no UUID, skipping", extra={'suppress_console': ENABLE_SUPPRESS})
                continue
            if r_item.get("type") == "folder":
                if r_name not in local_dir_names:
                    logging.info(f"Deleting remote-only folder: {normalize_rel_path(rel_cur_dir, r_name)}", extra={'suppress_console': ENABLE_SUPPRESS})
                    delete_remote_folder(normalize_rel_path(rel_cur_dir, r_name), r_uuid)
            else:
                if r_name not in local_file_keys:
                    try:
                        r_size = int(r_item.get("size", 0))
                    except Exception:
                        logging.warning(f"Invalid size for remote file '{normalize_rel_path(rel_cur_dir, r_name)}': {r_item.get('size')!r}, treating as 0", extra={'suppress_console': ENABLE_SUPPRESS})
                        r_size = 0
                    logging.info(f"Deleting remote-only file: {normalize_rel_path(rel_cur_dir, r_name)}", extra={'suppress_console': ENABLE_SUPPRESS})
                    delete_remote_file(normalize_rel_path(rel_cur_dir, r_name), r_uuid, r_size)

    # Step 6: check and upload each local file in this directory.
    for f in files:
        f_name = normalize_encoding(f)    # normalized name - used for remote key lookup and logging
        abs_path = os.path.join(cur_dir, f)  # raw OS name - must match actual filesystem entry
        rel_path = normalize_rel_path(rel_cur_dir, f_name)

        try:
            file_size = os.path.getsize(abs_path)
        except Exception:
            logging.error(f"Could not determine size of '{abs_path}'")
            num_failed_files += 1
            continue

        if file_size > FILE_SIZE_UPLOAD_LIMIT_BYTES:
            logging.info(f"File exceeds upload limit size ({format_size(FILE_SIZE_UPLOAD_LIMIT_BYTES)}, found {format_size(file_size)}), skipped: {rel_path}")
            continue

        # Accumulate local file size for this folder (only files within upload limit).
        folder_local_size[rel_cur_dir] = folder_local_size.get(rel_cur_dir, 0) + file_size

        # f_name is the composite remote key: "stem.ext" or "stem" for extension-less files.
        # This matches the key format built in list_remote_directory.
        remote_item = remote_items.get(f_name)

        # Guard: remote folder with same name as local file (name collision - skip safely).
        if remote_item is not None and remote_item.get("type") == "folder":
            logging.warning(f"Remote folder has same name as local file '{rel_path}', skipping file.")
            continue

        if remote_item is not None:
            # Fetch the remote size.
            try:
                remote_size = int(remote_item.get("size", 0))
            except Exception:
                logging.error(f"Invalid size format for file {rel_path}: {remote_item.get('size')!r}", extra={'suppress_console': ENABLE_SUPPRESS})
                remote_size = None

            # If the size matches, skip the file.
            if remote_size == file_size:
                logging.info(f"Skipped '{rel_path}' (same size)", extra={'suppress_console': ENABLE_SUPPRESS})
                skipped_files.append((rel_path, file_size))
                skipped_size += file_size
                processed_size += file_size
                continue
            # Otherwise, delete the remote file (= local file will be uploaded)
            elif args.allow_delete:
                logging.info(f"Remote '{rel_path}' has different size, replacing", extra={'suppress_console': ENABLE_SUPPRESS})
                delete_remote_file(rel_path, remote_item["uuid"], remote_size or 0)
                # fall through to upload
            else:
                if remote_size is None:
                    logging.warning(f"Remote file '{rel_path}' has unreadable size, keeping remote copy. Use --allow-delete to replace.")
                else:
                    logging.warning(f"Remote file '{rel_path}' has different size (remote: {format_size(remote_size)}, local: {format_size(file_size)}). Keeping remote copy. Use --allow-delete to replace.")
                skipped_files.append((rel_path, file_size))
                skipped_size += file_size
                processed_size += file_size
                continue

        # Show progress line before upload.
        elapsed_total = time.time() - upload_start_time
        if elapsed_upload_time > 0 and uploaded_size > 0:
            upload_rate = uploaded_size / elapsed_upload_time
            avg_speed_str = f"{upload_rate / 1024 / 1024:.2f} MB/s"
            last_speed_str = f"{last_file_mbps:.2f} MB/s"
        else:
            upload_rate = 0
            avg_speed_str = "-- MB/s"
            last_speed_str = "-- MB/s"
        if to_upload_size is not None:
            percent = uploaded_size / to_upload_size * 100 if to_upload_size else 0
            eta_str = format_hhmmss((to_upload_size - uploaded_size) / upload_rate) if upload_rate else "--:--:--"
            print_line(
                f"[{percent:5.1f}%] {format_size(uploaded_size)}/{format_size(to_upload_size)}"
                f" | ETA: {eta_str}"
                f" | {avg_speed_str} (last: {last_speed_str})"
                f" | elapsed: {format_hhmmss(elapsed_total)}"
                f" | failed: {num_failed_files}"
                f" | {rel_path} ({format_size(file_size)})"
            )
        else:
            print_line(
                f"[{processed_folders}/{total_folder_count} folders]"
                f" {format_size(uploaded_size)} uploaded"
                f" | {avg_speed_str} (last: {last_speed_str})"
                f" | elapsed: {format_hhmmss(elapsed_total)}"
                f" | failed: {num_failed_files}"
                f" | {rel_path} ({format_size(file_size)})"
            )

        # Upload the file.
        # SUPPRESS_STDOUT_STDERR hides the internxt CLI's own stdout/stderr during upload.
        # try/finally guarantees it is re-enabled even if run_cli raises.
        file_start = time.time()
        SUPPRESS_STDOUT_STDERR = True
        try:
            out, num_retries, _ = run_cli(
                ["upload-file", "-f", abs_path, f"--destination={folder_uuid}"],
                suppress_console_errors=ENABLE_SUPPRESS
            )
        finally:
            SUPPRESS_STDOUT_STDERR = False
        elapsed_file = time.time() - file_start

        if out is None:
            logging.error(f"upload-file failed, skipping '{rel_path}'")
            num_failed_files += 1
            continue

        if num_retries > 0:
            num_retried_files += 1
            num_total_retries += num_retries

        mbps = (file_size / 1024 / 1024) / elapsed_file if elapsed_file > 0 else 0
        last_file_mbps = mbps
        # Log upload to file only, with time and MB/s
        logging.info(f"Uploaded '{rel_path}' ({format_size(file_size)}) to folder UUID '{folder_uuid}' in {elapsed_file:.2f}s ({mbps:.2f} MB/s)", extra={'suppress_console': ENABLE_SUPPRESS})

        uploaded_files.append((rel_path, file_size))
        uploaded_size += file_size
        processed_size += file_size
        elapsed_upload_time += elapsed_file

        if args.progress_file:
            with open(args.progress_file, 'a', encoding='utf-8') as _pf:
                _pf.write(rel_path + '\n')

        # Invalidate folder cache since we modified it
        remote_dir_cache.pop(folder_uuid, None)

        # Per-folder stats
        stats = folder_upload_stats.setdefault(rel_cur_dir, {'size': 0, 'time': 0, 'files': 0})
        stats['size'] += file_size
        stats['time'] += elapsed_file
        stats['files'] += 1

logging.info(f"\nUpload finished. Elapsed time: {format_hhmmss(time.time() - upload_start_time)}")

################################################################################
# Summary
################################################################################

# End the progress line cleanly before printing multi-line summary.
sys.stdout.write('\n')
sys.stdout.flush()

logging.info(f"\nAll operations complete.")
logging.info(f"Folders created: {num_created_folders}")
logging.info(f"Folders removed: {len(removed_folders)}")
logging.info(f"Files uploaded:  {len(uploaded_files)} ({format_size(uploaded_size)})")
logging.info(f"Files skipped:   {len(skipped_files)}  ({format_size(skipped_size)})")
logging.info(f"Files retried:   {num_retried_files} ({num_total_retries} retries total)")
logging.info(f"Files failed:    {num_failed_files}")
logging.info(f"Files removed:   {len(removed_files)} ({format_size(removed_size)})")

# Log per-folder summary to log file
for folder, stats in folder_upload_stats.items():
    mbps = (stats['size'] / 1024 / 1024) / stats['time'] if stats['time'] > 0 else 0
    logging.info(f"Folder summary: '{folder}' | {stats['files']} files | {format_size(stats['size'])} | {stats['time']:.2f}s | {mbps:.2f} MB/s")

# Log per-folder size overview: local size vs remote size before upload vs uploaded size
logging.info(f"\nPer-folder size overview:", extra={'suppress_console': ENABLE_SUPPRESS})
all_folders = sorted(set(list(folder_local_size) + list(folder_remote_size)))
for folder in all_folders:
    local_sz    = folder_local_size.get(folder, 0)
    remote_sz   = folder_remote_size.get(folder, 0)
    uploaded_sz = folder_upload_stats.get(folder, {}).get('size', 0)
    logging.info(f"  '{folder}': local={format_size(local_sz)}, remote={format_size(remote_sz)}, uploaded={format_size(uploaded_sz)}", extra={'suppress_console': ENABLE_SUPPRESS})

logging.info(f"\nBackup successful. Total time: {format_hhmmss(time.time() - start_time)}")