"""Tests for the `importadded` plugin."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest

import beets.plugins
import beetsplug.importadded as importadded_plugin
from beets import importer, util
from beets.test.helper import AutotagImportTestCase, PluginMixin
from beetsplug.importadded import ImportAddedPlugin

if TYPE_CHECKING:
    from collections.abc import Iterable


def modify_mtimes(paths: Iterable[Path], offset=-60000):
    for i, path in enumerate(paths, start=1):
        mstat = path.stat()
        os.utime(path, (mstat.st_atime, mstat.st_mtime + offset * i))


class ImportAddedTest(PluginMixin, AutotagImportTestCase):
    # The minimum mtime of the files to be imported
    plugin = "importadded"
    min_mtime = None

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(2)
        # Different mtimes on the files to be imported in order to test the
        # plugin
        paths = [Path(mfile.path) for mfile in self.import_media]
        modify_mtimes(paths)
        self.min_mtime = min(p.stat().st_mtime for p in paths)
        self.importer = self.setup_importer()
        self.importer.add_choice(importer.Action.APPLY)

    def find_media_file_mtime(self, item) -> float:
        """Find the pre-import MediaFile for an Item"""
        for m in self.import_media:
            if m.title.replace("Tag", "Applied") == item.title:
                return Path(m.path).stat().st_mtime
        raise AssertionError(f"No MediaFile found for Item {item.filepath}")

    def test_import_album_with_added_dates(self):
        self.importer.run()

        album = self.lib.albums().get()
        assert album.added == self.min_mtime
        for item in album.items():
            assert item.added == self.min_mtime

    def test_import_album_inplace_with_added_dates(self):
        self.config["import"]["copy"] = False

        self.importer.run()

        album = self.lib.albums().get()
        assert album.added == self.min_mtime
        for item in album.items():
            assert item.added == self.min_mtime

    def test_import_album_with_preserved_mtimes(self):
        self.config["importadded"]["preserve_mtimes"] = True
        self.importer.run()
        album = self.lib.albums().get()
        assert album.added == self.min_mtime
        for item in album.items():
            assert item.added == pytest.approx(self.min_mtime, rel=1e-4)
            mediafile_mtime = self.find_media_file_mtime(item)
            assert item.mtime == pytest.approx(mediafile_mtime, rel=1e-4)
            assert item.filepath.stat().st_mtime == pytest.approx(
                mediafile_mtime, rel=1e-4
            )

    def test_reimported_album_skipped(self):
        # Import and record the original added dates
        self.importer.run()
        album = self.lib.albums().get()
        album_added_before = album.added
        items_added_before = {
            item.filepath: item.added for item in album.items()
        }
        # Newer Item path mtimes as if Beets had modified them
        modify_mtimes(items_added_before.keys(), offset=10000)
        # Reimport
        self.setup_importer(import_dir=self.lib_path)
        self.importer.run()
        # Verify the reimported items
        album = self.lib.albums().get()
        assert album.added == pytest.approx(album_added_before, rel=1e-4)
        items_added_after = {
            item.filepath: item.added for item in album.items()
        }
        for item_path, added_after in items_added_after.items():
            assert items_added_before[item_path] == pytest.approx(
                added_after, rel=1e-4
            ), f"reimport modified Item.added for {item_path}"

    def test_import_singletons_with_added_dates(self):
        self.config["import"]["singletons"] = True
        self.importer.run()
        for item in self.lib.items():
            assert item.added == pytest.approx(
                self.find_media_file_mtime(item), rel=1e-4
            )

    def test_import_singletons_with_preserved_mtimes(self):
        self.config["import"]["singletons"] = True
        self.config["importadded"]["preserve_mtimes"] = True
        self.importer.run()
        for item in self.lib.items():
            mediafile_mtime = self.find_media_file_mtime(item)
            assert item.added == pytest.approx(mediafile_mtime, rel=1e-4)
            assert item.mtime == pytest.approx(mediafile_mtime, rel=1e-4)
            assert item.filepath.stat().st_mtime == pytest.approx(
                mediafile_mtime, rel=1e-4
            )

    def test_reimported_singletons_skipped(self):
        self.config["import"]["singletons"] = True
        # Import and record the original added dates
        self.importer.run()
        items_added_before = {
            item.filepath: item.added for item in self.lib.items()
        }
        # Newer Item path mtimes as if Beets had modified them
        modify_mtimes(items_added_before.keys(), offset=10000)
        # Reimport
        self.setup_importer(import_dir=self.lib_path, singletons=True)
        self.importer.run()
        # Verify the reimported items
        items_added_after = {
            item.filepath: item.added for item in self.lib.items()
        }
        for item_path, added_after in items_added_after.items():
            assert items_added_before[item_path] == pytest.approx(
                added_after, rel=1e-4
            ), f"reimport modified Item.added for {item_path}"


class ImportAddedPreserveWriteMtimesTest(PluginMixin, AutotagImportTestCase):
    """Tests for the `preserve_write_mtimes` option.

    The timestamp recorded from the source file must end up on the file
    at its final location in the library directory, while the source
    file's own mtime is never modified.
    """

    plugin = "importadded"
    item_count = 2

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(self.item_count)
        self.src_paths = [Path(mfile.path) for mfile in self.import_media]
        modify_mtimes(self.src_paths)
        # Snapshot the source state before the import.
        self.src_mtimes = {p: p.stat().st_mtime for p in self.src_paths}
        self.config["importadded"]["preserve_write_mtimes"] = True
        self.importer = self.setup_importer()
        self.importer.add_choice(importer.Action.APPLY)

    def source_mtime_for(self, item) -> float:
        """Return the pre-import mtime of the source file for an item."""
        for media in self.import_media:
            if media.title.replace("Tag", "Applied") == item.title:
                return self.src_mtimes[Path(media.path)]
        raise AssertionError(f"No source file found for Item {item.filepath}")

    def assert_source_files_unchanged(self):
        for path, mtime in self.src_mtimes.items():
            if path.exists():
                assert path.stat().st_mtime == pytest.approx(mtime), (
                    f"import modified the mtime of source file {path}"
                )

    def assert_destination_mtimes_preserved(self):
        for item in self.lib.items():
            source_mtime = self.source_mtime_for(item)
            assert item.filepath.stat().st_mtime == pytest.approx(
                source_mtime
            ), f"destination mtime not preserved for {item.filepath}"
            assert item.mtime == pytest.approx(source_mtime, abs=1.5)

    def test_copy_preserves_destination_mtime(self):
        self.importer.run()
        self.assert_destination_mtimes_preserved()

    def test_copy_leaves_source_mtime_untouched(self):
        self.importer.run()
        self.assert_source_files_unchanged()

    def test_in_place_import_restores_source_mtime(self):
        # Source and destination are the same file: the metadata write
        # bumps the mtime, and the plugin must restore the file's own
        # original mtime afterwards rather than writing back `added`
        # (which is set only later, to the current time).
        self.config["import"]["copy"] = False
        self.importer.run()
        self.assert_source_files_unchanged()
        for item in self.lib.items():
            source_mtime = self.source_mtime_for(item)
            assert item.filepath.stat().st_mtime == pytest.approx(
                source_mtime
            )

    def test_copy_with_readonly_source_directory(self):
        # The option must not require write access to the source
        # directory: no timestamp is ever written back to a source file.
        for path in self.src_paths:
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(self.import_path / "album", 0o555)
        try:
            self.importer.run()
        finally:
            os.chmod(self.import_path / "album", 0o755)
            for path in self.src_paths:
                os.chmod(
                    path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP
                )
        self.assert_destination_mtimes_preserved()
        self.assert_source_files_unchanged()

    def test_move_preserves_mtime(self):
        self.config["import"]["copy"] = False
        self.config["import"]["move"] = True
        self.importer.run()
        # The source files were moved away, but their mtimes survived.
        assert not any(path.exists() for path in self.src_paths)
        self.assert_destination_mtimes_preserved()

    def test_move_across_devices_preserves_mtime(self):
        # Force the initial os.replace to fail with EXDEV so beets falls
        # back to copy-to-temp + delete, and verify the recorded mtime is
        # applied to the final destination rather than the vanished source.
        self.config["import"]["copy"] = False
        self.config["import"]["move"] = True
        replace_calls = {"count": 0}
        real_replace = os.replace

        def cross_device_replace(src, dst, *args, **kwargs):
            replace_calls["count"] += 1
            if replace_calls["count"] == 1:
                raise OSError(
                    errno.EXDEV, "cross-device link not permitted"
                )
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch(
            "beets.util.os.replace", side_effect=cross_device_replace
        ):
            self.importer.run()

        assert replace_calls["count"] >= 2
        self.assert_destination_mtimes_preserved()

    def test_existing_destination_is_not_overwritten(self):
        # When the standard destination already exists, beets imports to
        # a unique alternative path; the existing file must be untouched
        # and the mtime must be applied to the actual destination.
        blocker_dir = self.lib_path / "Applied Artist" / "Applied Album"
        blocker_dir.mkdir(parents=True)
        blocker = blocker_dir / "Applied Track 1.mp3"
        blocker.write_bytes(b"preexisting content")
        blocker_mtime = blocker.stat().st_mtime

        self.importer.run()

        assert blocker.read_bytes() == b"preexisting content"
        assert blocker.stat().st_mtime == pytest.approx(blocker_mtime)
        imported = [
            item
            for item in self.lib.items()
            if item.filepath != blocker
        ]
        assert imported
        for item in imported:
            source_mtime = self.source_mtime_for(item)
            assert item.filepath.stat().st_mtime == pytest.approx(
                source_mtime
            )

    def test_failed_move_rolls_back_recorded_mtime(self):
        # Simulate a failing rename: the source must stay in place with
        # its original mtime, and no timestamp may be recorded for a file
        # that never reached the library directory.
        self.config["import"]["copy"] = False
        self.config["import"]["move"] = True
        plugin = next(
            p
            for p in beets.plugins._instances
            if isinstance(p, ImportAddedPlugin)
        )

        def failing_move(path, dest, replace=False):
            raise OSError(errno.EACCES, "simulated move failure")

        with mock.patch.object(util, "move", side_effect=failing_move):
            with pytest.raises(OSError):
                self.importer.run()

        assert all(path.exists() for path in self.src_paths)
        self.assert_source_files_unchanged()
        assert plugin.item_mtime == {}

    def test_timestamp_restore_failure_does_not_abort_import(self):
        # If the mtime cannot be written to the final destination (e.g.
        # a read-only filesystem), the failure must be swallowed instead
        # of crashing the whole import: tag writing and database updates
        # still complete for every item.
        def failing_utime(path, times, *args, **kwargs):
            raise PermissionError(errno.EACCES, "read-only file system")

        with mock.patch.object(
            importadded_plugin.os, "utime", side_effect=failing_utime
        ):
            self.importer.run()

        assert len(list(self.lib.items())) == self.item_count
        self.assert_source_files_unchanged()


class ImportAddedPreserveWriteMtimesSingletonTest(
    PluginMixin, AutotagImportTestCase
):
    """Singleton variant of the write-mtime preservation tests."""

    plugin = "importadded"

    def setUp(self):
        super().setUp()
        self.prepare_album_for_import(1)
        self.src_path = Path(self.import_media[0].path)
        modify_mtimes([self.src_path])
        self.src_mtime = self.src_path.stat().st_mtime
        self.config["importadded"]["preserve_write_mtimes"] = True
        self.config["import"]["singletons"] = True
        self.importer = self.setup_importer(singletons=True)
        self.importer.add_choice(importer.Action.APPLY)

    def test_singleton_destination_mtime_preserved(self):
        self.importer.run()
        item = self.lib.items().get()
        assert item.filepath.stat().st_mtime == pytest.approx(
            self.src_mtime
        )
        assert self.src_path.stat().st_mtime == pytest.approx(
            self.src_mtime
        )

