import json
import logging
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


class TargetPathResolutionTest(unittest.TestCase):
    # The target is safety-critical, so argparse must reject an invocation that omits it
    # before authentication or any remote operation can occur.
    def test_target_is_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "2025")
            os.mkdir(source)
            argv = ["internxt_backup.py", "-s", source, "--no-prescan"]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch("subprocess.run") as run_cli, \
                    mock.patch("atexit.register"):
                with self.assertRaises(SystemExit) as exit_context:
                    runpy.run_path(os.path.join(os.path.dirname(__file__), "internxt_backup.py"), run_name="__main__")
            self.assertEqual(exit_context.exception.code, 2)
            run_cli.assert_not_called()

    # Exercise the script entry point with a fake CLI so the test verifies that a
    # named target is the parent and the source basename is selected beneath it.
    def test_named_target_is_resolved_as_destination_parent(self):
        parent_uuid = "11111111-1111-1111-1111-111111111111"
        source_uuid = "22222222-2222-2222-2222-222222222222"
        cli_commands = []

        # Return the smallest realistic Internxt responses needed by an empty backup.
        def fake_run(command, **kwargs):
            cli_commands.append(command)
            if '"whoami"' in command:
                payload = {"success": True}
            elif '"list"' in command and '"--id="' in command:
                payload = {
                    "success": True,
                    "list": {
                        "folders": [{"type": "folder", "plainName": "pictures", "uuid": parent_uuid}],
                        "files": [],
                    },
                }
            elif '"list"' in command and f'"--id={parent_uuid}"' in command:
                payload = {
                    "success": True,
                    "list": {
                        "folders": [{"type": "folder", "plainName": "2025", "uuid": source_uuid}],
                        "files": [],
                    },
                }
            elif '"list"' in command and f'"--id={source_uuid}"' in command:
                payload = {"success": True, "list": {"folders": [], "files": []}}
            else:
                self.fail(f"Unexpected CLI command: {command}")
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        # Run in a disposable directory because the script intentionally creates a log file.
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "2025")
            os.mkdir(source)
            old_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                argv = ["internxt_backup.py", "-s", source, "-t", "pictures", "--no-prescan"]
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch("subprocess.run", side_effect=fake_run), \
                        mock.patch("platform.system", return_value="Windows"), \
                        mock.patch("atexit.register"):
                    runpy.run_path(os.path.join(old_cwd, "internxt_backup.py"), run_name="__main__")
            finally:
                os.chdir(old_cwd)
                logging.shutdown()
                for handler in logging.getLogger().handlers[:]:
                    logging.getLogger().removeHandler(handler)
                    handler.close()

        self.assertFalse(any('"create-folder"' in command for command in cli_commands))
        self.assertTrue(any(f'"--id={source_uuid}"' in command for command in cli_commands))

    # A trailing wildcard changes only source-root placement: the resolved target
    # itself receives the files and no duplicate basename folder is created.
    def test_trailing_wildcard_copies_contents_into_target(self):
        parent_uuid = "11111111-1111-1111-1111-111111111111"
        target_uuid = "22222222-2222-2222-2222-222222222222"
        cli_commands = []

        # Model the exact /pictures/2024 hierarchy requested by the command line.
        def fake_run(command, **kwargs):
            cli_commands.append(command)
            if '"whoami"' in command:
                payload = {"success": True}
            elif '"list"' in command and '"--id="' in command:
                payload = {
                    "success": True,
                    "list": {
                        "folders": [{"type": "folder", "plainName": "pictures", "uuid": parent_uuid}],
                        "files": [],
                    },
                }
            elif '"list"' in command and f'"--id={parent_uuid}"' in command:
                payload = {
                    "success": True,
                    "list": {
                        "folders": [{"type": "folder", "plainName": "2024", "uuid": target_uuid}],
                        "files": [],
                    },
                }
            elif '"list"' in command and f'"--id={target_uuid}"' in command:
                payload = {"success": True, "list": {"folders": [], "files": []}}
            else:
                self.fail(f"Unexpected CLI command: {command}")
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        # Keep logs disposable while running the complete command-line entry point.
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "2024")
            os.mkdir(source)
            old_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                argv = ["internxt_backup.py", "-s", os.path.join(source, "*"), "-t", "pictures/2024", "--no-prescan"]
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch("subprocess.run", side_effect=fake_run), \
                        mock.patch("platform.system", return_value="Windows"), \
                        mock.patch("atexit.register"):
                    runpy.run_path(os.path.join(old_cwd, "internxt_backup.py"), run_name="__main__")
            finally:
                os.chdir(old_cwd)
                logging.shutdown()
                for handler in logging.getLogger().handlers[:]:
                    logging.getLogger().removeHandler(handler)
                    handler.close()

        self.assertFalse(any('"create-folder"' in command for command in cli_commands))
        self.assertTrue(any(f'"--id={target_uuid}"' in command for command in cli_commands))

    # A unique folder with the same recursive names and byte sizes should be renamed
    # in place, preserving its UUID and avoiding upload/delete operations.
    def test_matching_remote_folder_is_renamed_instead_of_reuploaded(self):
        pictures_uuid = "11111111-1111-1111-1111-111111111111"
        backup_uuid = "22222222-2222-2222-2222-222222222222"
        old_folder_uuid = "33333333-3333-3333-3333-333333333333"
        cli_commands = []

        # Model /pictures/backup/old-name with one four-byte file.
        def fake_run(command, **kwargs):
            cli_commands.append(command)
            if '"whoami"' in command:
                payload = {"success": True}
            elif '"list"' in command and '"--id="' in command:
                payload = {
                    "success": True,
                    "list": {
                        "folders": [{"type": "folder", "plainName": "pictures", "uuid": pictures_uuid}],
                        "files": [],
                    },
                }
            elif '"list"' in command and f'"--id={pictures_uuid}"' in command:
                payload = {
                    "success": True,
                    "list": {
                        "folders": [{"type": "folder", "plainName": "backup", "uuid": backup_uuid}],
                        "files": [],
                    },
                }
            elif '"list"' in command and f'"--id={backup_uuid}"' in command:
                payload = {
                    "success": True,
                    "list": {
                        "folders": [{"type": "folder", "plainName": "old-name", "uuid": old_folder_uuid}],
                        "files": [],
                    },
                }
            elif '"list"' in command and f'"--id={old_folder_uuid}"' in command:
                payload = {
                    "success": True,
                    "list": {
                        "folders": [],
                        "files": [{"type": "txt", "plainName": "same", "uuid": "file-uuid", "size": "4"}],
                    },
                }
            elif '"rename-folder"' in command and f'"--id={old_folder_uuid}"' in command and '"new-name"' in command:
                payload = {"success": True, "folder": {"uuid": old_folder_uuid, "plainName": "new-name"}}
            else:
                self.fail(f"Unexpected CLI command: {command}")
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

        # Run the real traversal against a local renamed folder with matching metadata.
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "backup")
            renamed = os.path.join(source, "new-name")
            os.makedirs(renamed)
            with open(os.path.join(renamed, "same.txt"), "w", encoding="ascii") as file_handle:
                file_handle.write("same")
            old_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                argv = ["internxt_backup.py", "-s", source, "-t", "pictures", "-d", "--no-prescan"]
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch("subprocess.run", side_effect=fake_run), \
                        mock.patch("platform.system", return_value="Windows"), \
                        mock.patch("atexit.register"):
                    runpy.run_path(os.path.join(old_cwd, "internxt_backup.py"), run_name="__main__")
            finally:
                os.chdir(old_cwd)
                logging.shutdown()
                for handler in logging.getLogger().handlers[:]:
                    logging.getLogger().removeHandler(handler)
                    handler.close()

        self.assertTrue(any('"rename-folder"' in command for command in cli_commands))
        self.assertFalse(any('"upload-file"' in command for command in cli_commands))
        self.assertFalse(any('"delete-permanently-folder"' in command for command in cli_commands))
        self.assertFalse(any('"create-folder"' in command for command in cli_commands))


if __name__ == "__main__":
    unittest.main()
