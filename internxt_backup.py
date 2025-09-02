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
from collections import defaultdict

# TODO: Validate that all files have been uploaded using "list"
# TODO: This is written against @internxt/cli/1.5.4 win32-x64 node-v22.18.0, validate version
# TODO: Validate sufficient remote space ("config" lists available / used space)

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
parser.add_argument("-t", "--target", dest="dest_id", required=True, help="Destination base folder UUID")
parser.add_argument("-v", "--verbose", dest="verbose_mode", action='store_true', help="Enable verbose logging")
parser.add_argument("-l", "--full-console-log", dest="full_console_log", action='store_true', help="log everything that is logged to file to the console as well")
parser.add_argument("-r", "--max_num_retries", dest="max_num_retries", required=False, default=5, type=int, help="Set the maximum number of retries for internxt CLI commands (default: 5)")
parser.add_argument("-w", "--retry_wait_seconds", dest="retry_wait_seconds", required=False, default=3, type=int, help="Set N, where N^{retry attempt} is the number of seconds to wait before the next retry (default: 3)")
parser.add_argument("-d", "--allow_delete", dest="allow_delete", action='store_true', help="Delete remote files/folders if they do not exist locally or are ignored")
parser.add_argument("-e", "--email", dest="email", required=False, help="Email for Internxt login")
parser.add_argument("-p", "--password", dest="password", required=False, help="Password for Internxt login (not recommended to use on CLI)")
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
    if cmd[1] != "login":
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
            result = subprocess.run(' '.join(cmd), shell=True, capture_output=True, text=True)
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
            assert(result.returncode != 0)
            logging.error(f"Command failed (invalid JSON) (attempt {attempt}): {' '.join(sanitized_cmd)}", extra={'suppress_console': suppress_console_errors})
            logging.error(result.stderr, extra={'suppress_console': suppress_console_errors})
            logging.error(result.stdout, extra={'suppress_console': suppress_console_errors})
            if attempt < cur_max_num_retries:
                time.sleep(RETRY_SLEEP_BASE_SECONDS ** attempt)
                continue
            return None, num_retries, False

        if out.get("success") is not True:
            msg = out.get("message")
            if stop_on_message is not None and stop_on_message in msg:
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
    result, num_retries, _ = run_cli(["login", f"-e={email}", f"-p={password}"])
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
    if 'logged_in' in globals() and logged_in:
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
    else:
        logging.debug("No logout needed (not logged in)")

# Register the graceful shutdown function to be called on normal exit
atexit.register(graceful_shutdown)

# Set up signal handler for graceful shutdown
def signal_handler(signum, frame):
    """Handle system signals for graceful shutdown."""
    logging.info(f"Received signal {signum}, initiating graceful shutdown...")
    graceful_shutdown()
    sys.exit(1)

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
        if "plainName" in item:  # Use decrypted name if available
            # Make sure we convert the result to UTF-8, otherwise file name matching is broken.
            items[normalize_encoding(item["plainName"])] = item
        else:
            items[item["name"]] = item
    for item in result.get("list", {}).get("files", []):
        if "plainName" in item:  # Use decrypted name if available
            # Make sure we convert the result to UTF-8, otherwise file name matching is broken.
            items[normalize_encoding(item["plainName"]) + "." + item['type']] = item
        else:
            items[item["name"]] = item

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
        logging.info(f"Found existing folder '{folder_name}' in '{parent_rel}' -> ID: {folder_uuid}")
        return folder_uuid

    # Create new folder if it doesn't exist
    out, num_retries, _ = run_cli(["create-folder", f"--id={parent_uuid}", f"--name={folder_name}"])

    if out is None:
        logging.error(f"create-folder failed, exiting")
        sys.exit(1)

    if num_retries > 0:
        logging.info(f"Create-folder command required {num_retries} retries: create-folder --id={parent_uuid} --name={folder_name}")

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
# Create list of local files/folders, compute sizes
################################################################################

all_local_files = []
all_local_folders = []
folder_subdir_map = defaultdict(list) # Maps a parent path -> list of subfolder names
file_sizes = {}
folder_sizes = {}
total_local_size = 0

for cur_dir, dirs, files in os.walk(SRC_DIR):
    cur_dir = normalize_encoding(cur_dir)
    rel_cur_dir = os.path.relpath(cur_dir, SRC_DIR)

    # Check for .internxtignore file and skip traversal
    if IGNOREFILE_NAME in files:
        logging.info(f"Folder contains {IGNOREFILE_NAME}, skipped: {rel_cur_dir}")
        dirs.clear()  # prevents walking into subdirectories
        continue

    all_local_folders.append(rel_cur_dir)

    # Add current dir as subfolder of its parent
    # If instead we added 'dirs' as subfolders of cur_dir we'd have to check .internxtignore files again
    if cur_dir != SRC_DIR:
        parent = os.path.dirname(rel_cur_dir)
        parent = '.' if parent == '' else parent
        child = os.path.basename(rel_cur_dir)
        folder_subdir_map[parent].append(child)

    folder_size = 0
    for file_name in files:
        file_name = normalize_encoding(file_name)
        abs_path = os.path.join(cur_dir, file_name)
        rel_path = normalize_rel_path(rel_cur_dir, file_name)

        try:
            file_size = os.path.getsize(abs_path)
        except Exception:
            logging.error(f"Could not determine size of file {abs_path}")
            file_size = 0

        # Skip files that exceed the upload limit
        if file_size > FILE_SIZE_UPLOAD_LIMIT_BYTES:
            logging.info(f"File exceeds upload limit size ({format_size(FILE_SIZE_UPLOAD_LIMIT_BYTES)}, found {format_size(file_size)}), skipped: {rel_path}")
            continue

        all_local_files.append((abs_path, rel_path, file_size))

        file_sizes[rel_path] = file_size
        folder_size += file_size

    folder_sizes[rel_cur_dir] = folder_size
    total_local_size += folder_size

# Log total size and folder sizes
logging.info(f"Total size of local folder(s): {format_size(total_local_size)}")
logging.info(f"Folder sizes:", extra={'suppress_console': ENABLE_SUPPRESS})
for folder, folder_size in folder_sizes.items():
    logging.info(f"  {folder}: {format_size(folder_size)}", extra={'suppress_console': ENABLE_SUPPRESS})

################################################################################
# Folder creation
# Create missing remote folders, collect all folder UUIDs
# Find all existing files that we don't have to upload again
################################################################################

created_folders = []
existing_folders = []
existing_files = {}
existing_size = 0
removed_folders = []
removed_files = []
removed_size = 0

# DFS traversal of remote folder tree to create missing folders
stack = [(".", DEST_ROOT_ID)]
existing_folders.append((".", DEST_ROOT_ID))

def delete_remote_folder(rel_path, folder_uuid):
    global removed_size
    # TODO: Calculate the size of the folder. This can be an expensive operation so we don't do it for now.
    # folder_size = calculate_remote_folder_size(rel_path, folder_uuid)
    folder_size = 0

    # Delete the folder.
    out, _, _ = run_cli(["delete-permanently-folder", f"--id={folder_uuid}"], suppress_console_errors=ENABLE_SUPPRESS)
    if out is None:
        logging.error(f"Failed to delete folder {rel_path}", extra={'suppress_console': ENABLE_SUPPRESS})
    else:
        # Update stats after deletion.
        removed_folders.append(rel_path)
        removed_size += folder_size
        # Remove folder from cache.
        remote_dir_cache.pop(folder_uuid, None)

def delete_remote_file(rel_path, file_uuid, file_size):
    global removed_size
    out, _, _ = run_cli(["delete-permanently-file", f"--id={file_uuid}"], suppress_console_errors=ENABLE_SUPPRESS)
    if out is None:
        logging.error(f"Failed to delete file {rel_path}", extra={'suppress_console': ENABLE_SUPPRESS})
    else:
        # Update stats after deletion.
        removed_files.append(rel_path)
        removed_size += file_size

while stack:
    rel_cur_dir, folder_uuid = stack.pop()

    # Delete remote folders not present locally
    if rel_cur_dir not in folder_sizes:
        if args.allow_delete:
            logging.info(f"Deleting remote folder '{rel_cur_dir}' since it does not exist locally or is ignored", extra={'suppress_console': ENABLE_SUPPRESS})
            delete_remote_folder(rel_cur_dir, folder_uuid)
        continue

    folder_uuids[rel_cur_dir] = folder_uuid

    folder_items = get_cached_dir_listing(folder_uuid)

    # Initialize missing folders to the set of all subdirs
    # We remove all folders that we also find remotely.
    missing_subfolders = set(folder_subdir_map.get(rel_cur_dir, []))

    # Check existing files/folders.
    for name, metadata in folder_items.items():
        name = normalize_encoding(name)
        rel_path = normalize_rel_path(rel_cur_dir, name)

        if metadata.get("type") == "folder":
            subfolder_uuid = metadata.get("uuid")
            if not subfolder_uuid:
                logging.error(f"Could not find UUID for folder '{rel_path}'", extra={'suppress_console': ENABLE_SUPPRESS})
                continue

            if rel_path in folder_sizes:
                # The remote folder is also present locally -> recurse.
                stack.append((rel_path, subfolder_uuid))
                existing_folders.append((rel_path, subfolder_uuid))
                # Remove existing folder from the "missing" list.
                missing_subfolders.remove(name)
            elif args.allow_delete:
                # The remote folder does not exist locally -> delete it.
                logging.info(f"Deleting remote folder '{rel_path}' since it does not exist locally or is ignored", extra={'suppress_console': ENABLE_SUPPRESS})
                delete_remote_folder(rel_path, subfolder_uuid)
        else:
            file_uuid = metadata.get("uuid")
            if not file_uuid:
                logging.error(f"Could not find UUID for file '{rel_path}'", extra={'suppress_console': ENABLE_SUPPRESS})
                continue

            # Fetch the remote size.
            try:
                remote_size = int(metadata.get("size", 0))
            except Exception:
                logging.error(f"Invalid size format for file {rel_path}: {metadata.get('size')}", extra={'suppress_console': ENABLE_SUPPRESS})
                continue

            # Fetch the local size. Returns None if the file does not exist locally.
            local_size = file_sizes.get(rel_path)
            if local_size is None:
                if args.allow_delete:
                    # The remote file does not exist locally -> delete it.
                    logging.info(f"Deleting remote file '{rel_path}' since it does not exist locally", extra={'suppress_console': ENABLE_SUPPRESS})
                    delete_remote_file(rel_path, file_uuid, remote_size)
                continue

            # If the size matches, skip the file.
            # Otherwise, delete the remote file (= local file will be uploaded)
            if remote_size == local_size:
                logging.info(f"Skipped file '{rel_path}' (same size)", extra={'suppress_console': ENABLE_SUPPRESS})
                existing_size += local_size
                existing_files[rel_path] = file_uuid
            else:
                if args.allow_delete:
                    logging.info(f"Remote file '{rel_path}' has different size, deleting", extra={'suppress_console': ENABLE_SUPPRESS})
                    delete_remote_file(rel_path, file_uuid, remote_size)
                else:
                    logging.info(f"Skipped file '{rel_path}' (different size, overwrite disabled)", extra={'suppress_console': ENABLE_SUPPRESS})
                    existing_size += local_size
                    existing_files[rel_path] = file_uuid

    # Create missing subfolders
    for name in missing_subfolders:
        subfolder_uuid = get_or_create_folder(folder_items, folder_uuid, name, rel_cur_dir)
        rel_path = normalize_rel_path(rel_cur_dir, name)
        created_folders.append((rel_path, subfolder_uuid))
        stack.append((rel_path, subfolder_uuid))

logging.info(f"\nFolder setup successful.")
logging.info(f"Created {len(created_folders)} new folders.")
logging.info(f"Found {len(existing_files)} existing files in {len(existing_folders)} (sub-)folders")
logging.info(f"Skipped {len(existing_files)}, size {format_size(existing_size)}.")
logging.info(f"Removed {len(removed_folders)} folders (with all contained files and subfolders) and {len(removed_files)} files, size {format_size(removed_size)} (w/o folder size).")

# Adjust total upload size to account for skipped files
num_bytes_to_upload = total_local_size - existing_size
logging.info(f"Total size to upload without skipped files: {format_size(num_bytes_to_upload)}")

################################################################################
# Progress bar
################################################################################

def print_progress_bar(uploaded, total, file, file_size, total_time, num_retried_files, num_failed_files, bar_len=40):
    percent = uploaded / total if total else 0
    filled_len = int(round(bar_len * percent))
    bar = '=' * filled_len + '-' * (bar_len - filled_len)
    speed = uploaded / total_time if total_time > 0 else 0

    # Create the output string
    output = (
        f"\r[{bar}] {percent * 100:5.1f}% "
        f"| {format_size(uploaded)}/{format_size(total)} "
        f"| retried: {num_retried_files} "
        f"| failed: {num_failed_files} "
        f"| avg: {format_size(speed)}/s "
        f"| elapsed: {format_hhmmss(total_time)} "
        f"| {file} "
        f"| file size: {format_size(file_size)}"
    )

    # Print the output
    sys.stdout.write(output)
    sys.stdout.flush()

    # Clear the line if the next file name is shorter
    # This is done by moving the cursor back to the start of the line
    # and writing spaces to overwrite the previous output
    sys.stdout.write('\r' + ' ' * (len(output) - 1) + '\r')

################################################################################
# File upload
################################################################################

uploaded_size = 0
uploaded_files = []

# Retry stats
num_retried_files = 0
num_total_retries = 0
num_failed_files = 0

# For per-folder stats
folder_upload_stats = {}

num_files_for_upload = len(all_local_files) - len(existing_files)
num_folders_for_upload = len(all_local_folders) - len(existing_folders)
logging.info(f"Processing {num_files_for_upload} files in {num_folders_for_upload} (sub-)folders, total size: {format_size(num_bytes_to_upload)}.")

start_time = time.time()

# From here on, don't print anything except the progress bar to stdout/stderr,
# logging only goes to file.
SUPPRESS_STDOUT_STDERR = True

for abs_path, rel_path, file_size in all_local_files:
    dest_folder_rel = os.path.dirname(rel_path) if rel_path != '.' else '.'
    dest_folder_uuid = folder_uuids.get(dest_folder_rel, DEST_ROOT_ID)
    file_name = os.path.basename(rel_path)

    # Skip the upload if file already exists.
    existing_file = existing_files.get(rel_path)
    if existing_file:
        elapsed_total = time.time() - start_time
        print_progress_bar(uploaded_size, num_bytes_to_upload, f"{rel_path} [SKIP]", file_size, elapsed_total, num_retried_files, num_failed_files)
        continue

    # Print progress bar *before* upload so we see what's currently uploading.
    elapsed_total = time.time() - start_time
    print_progress_bar(uploaded_size, num_bytes_to_upload, rel_path, file_size, elapsed_total, num_retried_files, num_failed_files)

    # Upload the file.
    file_start = time.time()
    out, num_retries, _ = run_cli(["upload-file", "-f", abs_path, f"--destination={dest_folder_uuid}"], suppress_console_errors=ENABLE_SUPPRESS)
    elapsed_file = time.time() - file_start

    if out is None:
        logging.error(f"upload-file failed, skipping {rel_path}")
        num_failed_files += 1
        continue

    if num_retries > 0:
        num_retried_files += 1
        num_total_retries += num_retries

    # Log upload to file only, with time and MB/s
    mbps = (file_size / 1024 / 1024) / elapsed_file if elapsed_file > 0 else 0
    action = "Updated" if existing_file is not None else "Uploaded"
    logging.info(f"{action} file '{rel_path}' ({format_size(file_size)}) to folder UUID '{dest_folder_uuid}' in {elapsed_file:.2f}s ({mbps:.2f} MB/s)")
    uploaded_files.append((rel_path, file_size))
    uploaded_size += file_size
    # Invalidate folder cache since we modified it
    remote_dir_cache.pop(dest_folder_uuid, None)

    # Per-folder stats
    stats = folder_upload_stats.setdefault(dest_folder_rel, {'size': 0, 'time': 0, 'files': 0})
    stats['size'] += file_size
    stats['time'] += elapsed_file
    stats['files'] += 1

################################################################################
# Re-enable stdout/stderr logging.
SUPPRESS_STDOUT_STDERR = False
################################################################################

logging.info("\nAll operations complete.")
logging.info(f"Folders created: {len(created_folders)}")
logging.info(f"Folders removed: {len(removed_folders)}")
logging.info(f"Files uploaded:  {len(uploaded_files)} ({format_size(uploaded_size)})")
logging.info(f"Files skipped:   {len(existing_files)} ({format_size(existing_size)})")
logging.info(f"Files retried:   {num_retried_files} ({num_total_retries} retries total)")
logging.info(f"Files failed:    {num_failed_files}")
logging.info(f"Files removed:   {len(removed_files)} ({format_size(removed_size)})")

# Log per-folder summary to log file
for folder, stats in folder_upload_stats.items():
    mbps = (stats['size'] / 1024 / 1024) / stats['time'] if stats['time'] > 0 else 0
    logging.info(f"Folder summary: '{folder}' | {stats['files']} files | {format_size(stats['size'])} | {stats['time']:.2f}s | {mbps:.2f} MB/s")

logging.info(f"Backup successful.")

# Ensure graceful shutdown on normal completion
graceful_shutdown()
