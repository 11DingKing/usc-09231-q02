"""Tests for the `importadded` plugin."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from beets import importer, plugins
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

    # Tests for `preserve_write_mtimes`: the timestamp of the source file
    # must be applied to the file written into the library directory, and
    # the source file itself must never be modified.

    def source_paths(self) -> list[Path]:
        return [Path(mfile.path) for mfile in self.import_media]

    def source_mtimes_by_title(self) -> dict[str, float]:
        """Map the applied (post-import) title to the source file mtime.

        Unlike find_media_file_mtime this also works when the source file
        no longer exists, e.g. after a move import.
        """
        return {
            m.title.replace("Tag", "Applied"): Path(m.path).stat().st_mtime
            for m in self.import_media
        }

    def importadded_plugin(self) -> ImportAddedPlugin:
        return next(
            p for p in plugins._instances if isinstance(p, ImportAddedPlugin)
        )

    def assert_sources_untouched(self, paths, contents, mtimes):
        for path in paths:
            assert path.exists(), f"source disappeared: {path}"
            assert (
                path.read_bytes() == contents[path]
            ), f"source content modified: {path}"
            assert (
                path.stat().st_mtime == mtimes[path]
            ), f"source mtime modified: {path}"

    def test_import_copy_preserve_write_mtimes(self):
        self.config["importadded"]["preserve_write_mtimes"] = True
        paths = self.source_paths()
        contents = {p: p.read_bytes() for p in paths}
        mtimes = {p: p.stat().st_mtime for p in paths}

        self.importer.run()

        # The source files must be left completely untouched
        self.assert_sources_untouched(paths, contents, mtimes)
        # The files written to the library carry their source's mtime
        album = self.lib.albums().get()
        assert album.added == self.min_mtime
        for item in album.items():
            source_mtime = self.find_media_file_mtime(item)
            assert item.added == self.min_mtime
            assert item.filepath.stat().st_mtime == pytest.approx(
                source_mtime, abs=2
            )
            assert item.mtime == pytest.approx(source_mtime, abs=2)

    def test_import_singletons_preserve_write_mtimes(self):
        self.config["import"]["singletons"] = True
        self.config["importadded"]["preserve_write_mtimes"] = True
        paths = self.source_paths()
        contents = {p: p.read_bytes() for p in paths}
        mtimes = {p: p.stat().st_mtime for p in paths}

        self.importer.run()

        self.assert_sources_untouched(paths, contents, mtimes)
        for item in self.lib.items():
            source_mtime = self.find_media_file_mtime(item)
            assert item.added == pytest.approx(source_mtime, abs=2)
            assert item.filepath.stat().st_mtime == pytest.approx(
                source_mtime, abs=2
            )

    def test_import_move_preserve_write_mtimes(self):
        self.config["import"]["move"] = True
        self.config["importadded"]["preserve_write_mtimes"] = True
        source_mtimes = self.source_mtimes_by_title()

        self.importer.run()

        # The sources were moved away, the library files carry their mtimes
        for path in self.source_paths():
            assert not path.exists()
        for item in self.lib.items():
            assert item.filepath.stat().st_mtime == pytest.approx(
                source_mtimes[item.title], abs=2
            )

    def test_import_move_across_devices_preserve_write_mtimes(self):
        self.config["import"]["move"] = True
        self.config["importadded"]["preserve_write_mtimes"] = True
        source_mtimes = self.source_mtimes_by_title()
        sources = {str(p) for p in self.source_paths()}
        real_replace = os.replace

        def exdev_replace(src, dst, *args, **kwargs):
            # Simulate a source and library on different filesystems
            if os.fspath(src) in sources:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch("os.replace", exdev_replace):
            self.importer.run()

        for path in self.source_paths():
            assert not path.exists()
        for item in self.lib.items():
            assert item.filepath.stat().st_mtime == pytest.approx(
                source_mtimes[item.title], abs=2
            )

    def test_import_inplace_preserve_write_mtimes(self):
        self.config["import"]["copy"] = False
        self.config["importadded"]["preserve_write_mtimes"] = True
        paths = self.source_paths()
        mtimes = {p: p.stat().st_mtime for p in paths}

        self.importer.run()

        # The metadata write of the import must not clobber the mtimes of
        # the in-place imported files
        for path in paths:
            assert path.stat().st_mtime == mtimes[path]
        album = self.lib.albums().get()
        assert album.added == self.min_mtime
        for item in album.items():
            assert item.added == self.min_mtime
            assert item.mtime == pytest.approx(
                mtimes[Path(item.filepath)], abs=2
            )

    def test_import_inplace_readonly_source_no_failure(self):
        self.config["import"]["copy"] = False
        self.config["importadded"]["preserve_mtimes"] = True
        self.config["importadded"]["preserve_write_mtimes"] = True
        sources = {str(p) for p in self.source_paths()}
        real_utime = os.utime

        def readonly_utime(path, *args, **kwargs):
            # Simulate a read-only source directory: writing timestamps
            # back to the source files is denied
            if os.fspath(path) in sources:
                raise PermissionError(
                    errno.EACCES, "Permission denied", path
                )
            return real_utime(path, *args, **kwargs)

        with mock.patch("os.utime", readonly_utime):
            # The import must not fail even though the source mtimes
            # cannot be restored
            self.importer.run()

        album = self.lib.albums().get()
        assert album.added == self.min_mtime
        for item in album.items():
            assert item.added == self.min_mtime

    def test_import_hardlink_preserve_write_mtimes(self):
        self.config["import"]["hardlink"] = True
        self.config["importadded"]["preserve_write_mtimes"] = True
        paths = self.source_paths()
        mtimes = {p: p.stat().st_mtime for p in paths}

        self.importer.run()

        # The hardlinked library files share the sources' inodes, so the
        # original mtimes must be preserved on both
        for path in paths:
            assert path.stat().st_mtime == mtimes[path]
        for item in self.lib.items():
            assert item.filepath.stat().st_mtime == pytest.approx(
                self.find_media_file_mtime(item), abs=2
            )

    def test_import_copy_over_existing_destination(self):
        self.config["importadded"]["preserve_write_mtimes"] = True
        # Pre-existing files where the imported files would land
        album_dir = self.lib_path / "Applied Artist" / "Applied Album"
        album_dir.mkdir(parents=True)
        existing = {}
        for title in ("Applied Track 1", "Applied Track 2"):
            path = album_dir / f"{title}.mp3"
            path.write_bytes(f"sentinel {title}".encode())
            os.utime(path, (1600000000.0, 1600000000.0))
            existing[path] = f"sentinel {title}".encode()

        self.importer.run()

        # The pre-existing files are neither overwritten nor retimestamped
        for path, content in existing.items():
            assert path.read_bytes() == content
            assert path.stat().st_mtime == 1600000000.0
        # The imports landed on de-duplicated paths, carrying the mtimes
        # recorded from their source files
        for item in self.lib.items():
            assert item.filepath.name.endswith(".1.mp3")
            assert item.filepath.stat().st_mtime == pytest.approx(
                self.find_media_file_mtime(item), abs=2
            )

    def test_preserve_write_mtimes_stable_after_later_write(self):
        self.config["importadded"]["preserve_write_mtimes"] = True
        self.importer.run()

        for item in self.lib.items():
            # A later metadata write, e.g. via `beet write`, must set the
            # file mtime to the item's added date, keeping it stable
            item.write()
            assert item.filepath.stat().st_mtime == pytest.approx(
                item.added, abs=2
            )
            assert item.mtime == pytest.approx(item.added, abs=2)

    def test_skipped_import_leaves_no_stale_state(self):
        self.config["import"]["copy"] = False
        self.importer.clear_choices()
        self.importer.add_choice(importer.Action.SKIP)
        self.importer.run()
        plugin = self.importadded_plugin()
        # The in-place import recorded mtimes that were never consumed
        assert plugin.item_mtime
        assert not self.lib.items()

        # A new import session must not inherit the stale recorded mtimes
        self.setup_importer()
        self.importer.add_choice(importer.Action.APPLY)
        self.importer.run()

        assert plugin.item_mtime == {}
        album = self.lib.albums().get()
        assert album.added == self.min_mtime

    def test_reimport_leaves_no_stale_state(self):
        self.importer.run()
        assert self.importadded_plugin().item_mtime == {}

        # Reimport from the library directory
        self.setup_importer(import_dir=self.lib_path)
        self.importer.run()

        # The mtimes recorded for the reimported items were discarded
        assert self.importadded_plugin().item_mtime == {}
