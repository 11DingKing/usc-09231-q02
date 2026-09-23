"""Populate an item's `added` and `mtime` fields by using the file
modification time (mtime) of the item's source file before import.

Reimported albums and items are skipped.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from beets import importer, util
from beets.plugins import BeetsPlugin

if TYPE_CHECKING:
    from beets.importer import ImportSession, ImportTask
    from beets.library import Album, Item, Library


class ImportAddedPlugin(BeetsPlugin):
    item_mtime: dict[bytes, float]
    reimported_item_ids: set[int | None]
    replaced_album_paths: set[bytes]

    def __init__(self) -> None:
        super().__init__()
        self.config.add(
            {"preserve_mtimes": False, "preserve_write_mtimes": False}
        )

        # item.id for new items that were reimported
        self.reimported_item_ids = set()
        # album.path for old albums that were replaced by a reimported album
        self.replaced_album_paths = set()
        # item path in the library to the mtime of the source file
        self.item_mtime = {}

        register = self.register_listener
        register("import_begin", self.reset_import_state)
        register("import_task_created", self.check_config)
        register("import_task_created", self.record_if_inplace)
        register("import_task_files", self.record_reimported)
        register("before_item_moved", self.record_import_mtime)
        register("item_copied", self.record_import_mtime)
        register("item_linked", self.record_import_mtime)
        register("item_hardlinked", self.record_import_mtime)
        register("item_reflinked", self.record_import_mtime)
        register("album_imported", self.update_album_times)
        register("item_imported", self.update_item_times)
        register("after_write", self.update_after_write_time)

    def check_config(
        self, task: ImportTask, session: ImportSession
    ) -> list[ImportTask] | None:
        self.config["preserve_mtimes"].get(bool)
        return None

    def reset_import_state(self, session: ImportSession) -> None:
        """Drop state that a previous import session may have left behind.

        Source mtimes are recorded when a file is copied, moved or linked
        into the library and consumed once the item or album import
        finishes. If a task is skipped or the import aborts between those
        two points, the recorded entries must not linger and leak into a
        later session, where they could be applied to unrelated files.
        """
        self.item_mtime.clear()
        self.reimported_item_ids.clear()
        self.replaced_album_paths.clear()

    def reimported_item(self, item: Item) -> bool:
        return item.id in self.reimported_item_ids

    def reimported_album(self, album: Album) -> bool:
        return album.path in self.replaced_album_paths

    def record_if_inplace(
        self, task: ImportTask, session: ImportSession
    ) -> list[ImportTask] | None:
        if not (
            session.config["copy"]
            or session.config["move"]
            or session.config["link"]
            or session.config["hardlink"]
            or session.config["reflink"]
        ):
            self._log.debug(
                "In place import detected, recording mtimes from source paths"
            )
            items = (
                [task.item]
                if isinstance(task, importer.SingletonImportTask)
                else task.items
            )
            for item in items:
                self.record_import_mtime(item, item.path, item.path)

        return None

    def record_reimported(
        self, task: ImportTask, session: ImportSession
    ) -> None:
        self.reimported_item_ids = {
            item.id
            for item, replaced_items in task.replaced_items.items()
            if replaced_items
        }
        self.replaced_album_paths = set(task.replaced_albums.keys())

    def write_file_mtime(self, path: bytes, mtime: float) -> None:
        """Write the given mtime to the destination path."""
        stat = os.stat(util.syspath(path))
        os.utime(util.syspath(path), (stat.st_atime, mtime))

    def write_item_mtime(
        self, item: Item, mtime: float, path: bytes | None = None
    ) -> None:
        """Write the given mtime to an item's `mtime` field and to the mtime
        of the item's file.

        `path` is the file the mtime is applied to and defaults to the
        item's path. A file that cannot be modified, for example a
        read-only source of an in-place import, is skipped with a warning
        instead of aborting the import.
        """
        if path is None:
            path = item.path
        try:
            self.write_file_mtime(path, mtime)
        except OSError as exc:
            self._log.warning(
                "Failed to write mtime {} to '{}': {}",
                mtime,
                util.displayable_path(path),
                exc,
            )
            return
        # The file's mtime on disk must be in sync with the item's mtime
        if path == item.path:
            item.mtime = int(mtime)

    def record_import_mtime(
        self, item: Item, source: bytes, destination: bytes
    ) -> None:
        """Record the file mtime of an item's path before its import."""
        mtime = os.stat(util.syspath(source)).st_mtime
        self.item_mtime[destination] = mtime
        self._log.debug(
            "Recorded mtime {} for item '{}' imported from '{}'",
            mtime,
            util.displayable_path(destination),
            util.displayable_path(source),
        )

    def update_album_times(self, lib: Library, album: Album) -> None:
        reimported = self.reimported_album(album)
        if reimported:
            self._log.debug(
                "Album '{.filepath}' is reimported, skipping import of "
                "added dates for the album and its items.",
                album,
            )

        album_mtimes = []
        for item in album.items():
            # Pop every recorded mtime, also for reimported albums: their
            # entries must be discarded, not left behind in item_mtime.
            mtime = self.item_mtime.pop(item.path, None)
            if reimported or not mtime:
                continue
            album_mtimes.append(mtime)
            if self.config["preserve_mtimes"].get(bool):
                self.write_item_mtime(item, mtime)
                item.store()
        if reimported:
            return

        if album_mtimes:
            album.added = min(album_mtimes)
        self._log.debug(
            "Import of album '{0.album}', selected album.added={0.added} "
            "from item file mtimes.",
            album,
        )
        album.store()

    def update_item_times(self, lib: Library, item: Item) -> None:
        # Pop the recorded mtime even for reimported items so that their
        # entries are discarded rather than left behind in item_mtime.
        mtime = self.item_mtime.pop(item.path, None)
        if self.reimported_item(item):
            self._log.debug(
                "Item '{.filepath}' is reimported, skipping import of added date.",
                item,
            )
            return
        if mtime:
            item.added = mtime
            if self.config["preserve_mtimes"].get(bool):
                self.write_item_mtime(item, mtime)
            self._log.debug(
                "Import of item '{0.filepath}', selected item.added={0.added}",
                item,
            )
            item.store()

    def update_after_write_time(self, item: Item, path: bytes) -> None:
        """Update the mtime of the written file after each write of the
        item if `preserve_write_mtimes` is enabled.

        The timestamp is applied to the file that was actually written
        (the event's `path`), which during an import is the final file in
        the library directory -- never the source file. If a source mtime
        was recorded for that path, the write is part of an import and
        the recorded mtime is applied; otherwise the item's `added` date
        is used.
        """
        if not self.config["preserve_write_mtimes"].get(bool):
            return
        mtime = self.item_mtime.get(path)
        if mtime is None:
            mtime = item.added
        if not mtime:
            return
        self.write_item_mtime(item, mtime, path)
        self._log.debug(
            "Write of item '{0.filepath}', applied mtime {1} to '{2}'",
            item,
            mtime,
            util.displayable_path(path),
        )
