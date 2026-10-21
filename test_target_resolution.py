import uuid
from types import SimpleNamespace

import pytest

import internxt_backup


def folder(name, folder_uuid):
    return internxt_backup.RemoteItem("folder", folder_uuid, name, None,
                                      {"uuid": folder_uuid, "plainName": name, "type": "folder"})


def test_target_is_required_before_any_remote_work():
    parser = internxt_backup.build_parser()
    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["--source", "."])
    assert exit_info.value.code == 2


def test_named_target_resolves_exact_drive_root_relative_path():
    pictures_uuid = str(uuid.uuid4())
    year_uuid = str(uuid.uuid4())
    listings = {
        "": internxt_backup.RemoteDirectory(folders={"pictures": folder("pictures", pictures_uuid)}),
        pictures_uuid: internxt_backup.RemoteDirectory(folders={"2025": folder("2025", year_uuid)}),
    }

    class DummyBackup:
        args = SimpleNamespace(target="pictures/2025")
        def list_remote(self, folder_uuid, path):
            return listings[folder_uuid]

    assert internxt_backup.Backup.resolve_target_parent(DummyBackup()) == year_uuid


def test_target_path_rejects_parent_traversal():
    class DummyBackup:
        args = SimpleNamespace(target="pictures/../outside")
        def list_remote(self, *_args):
            raise AssertionError("invalid path must be rejected before listing")

    with pytest.raises(internxt_backup.BackupError):
        internxt_backup.Backup.resolve_target_parent(DummyBackup())


def test_source_root_and_contents_placement_semantics(tmp_path):
    source = tmp_path / "2025 with spaces"
    source.mkdir()
    root, name, copy_contents = internxt_backup.source_details(str(source))
    assert root == source.resolve()
    assert name == "2025 with spaces"
    assert copy_contents is False

    contents_root, contents_name, copy_contents = internxt_backup.source_details(str(source / "*"))
    assert contents_root == source.resolve()
    assert contents_name == source.name
    assert copy_contents is True
