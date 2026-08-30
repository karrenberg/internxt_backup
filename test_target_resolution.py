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

    # Exercise the script entry point with a fake CLI so the test verifies both path
    # resolution and the crucial absence of an extra nested source folder.
    def test_named_target_is_resolved_from_root_and_used_directly(self):
        target_uuid = "11111111-1111-1111-1111-111111111111"
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
                        "folders": [{"type": "folder", "plainName": "2025", "uuid": target_uuid}],
                        "files": [],
                    },
                }
            elif '"list"' in command and f'"--id={target_uuid}"' in command:
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
                argv = ["internxt_backup.py", "-s", source, "-t", "2025", "--no-prescan"]
                with mock.patch.object(sys, "argv", argv), \
                        mock.patch("subprocess.run", side_effect=fake_run), \
                        mock.patch("platform.system", return_value="Windows"), \
                        mock.patch("atexit.register"):
                    runpy.run_path(os.path.join(old_cwd, "internxt_backup.py"), run_name="__main__")
            finally:
                os.chdir(old_cwd)
                logging.shutdown()

        self.assertFalse(any('"create-folder"' in command for command in cli_commands))
        self.assertTrue(any(f'"--id={target_uuid}"' in command for command in cli_commands))


if __name__ == "__main__":
    unittest.main()
