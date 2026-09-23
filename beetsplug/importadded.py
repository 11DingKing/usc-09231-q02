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
    # mtimes captured before a rename, committed to `item_mtime` only once
    # the rename has actually succeeded
    pending_mtime: dict[bytes, float]
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
        # destination path to the source mtime captured before a move;
        # entries are committed when the matching `item_moved` event fires
        self.pending_mtime = {}

        register = self.register_listener
        register("import_task_created", self.check_config)
        register("import_task_created", self.record_if_inplace)
        register("import_task_files", self.record_reimported)
        register("before_item_moved", self.record_before_move)
        register("item_moved", self.record_after_move)
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
        # A new task starts after the previous one finished, so any mtime
        # captured before a move that never completed (a failed rename that
        # aborted the earlier task) can no longer be committed: roll it back.
        self.pending_mtime.clear()
        return None

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
        # Any pending move never completed (its task is done either way),
        # so discard the mtimes captured before the failed rename.
        self.pending_mtime.clear()

        self.reimported_item_ids = {
            item.id
            for item, replaced_items in task.replaced_items.items()
            if replaced_items
        }
        self.replaced_album_paths = set(task.replaced_albums.keys())

    def write_file_mtime(self, path: bytes | str, mtime: float) -> None:
        """Write the given mtime to the destination path."""
        stat = os.stat(util.syspath(path))
        os.utime(util.syspath(path), (stat.st_atime, mtime))

    def write_item_mtime(self, item: Item, mtime: float) -> None:
        """Write the given mtime to an item's `mtime` field and to the mtime
        of the item's file.
        """
        # The file's mtime on disk must be in sync with the item's mtime
        self.write_file_mtime(util.syspath(item.path), mtime)
        item.mtime = int(mtime)

    def apply_file_mtime(
        self, item: Item, path: bytes, mtime: float
    ) -> bool:
        """Apply a recorded mtime to a file after metadata was written.

        The timestamp is only applied to the given (final destination)
        path -- never to a source file.  Failures (e.g. a read-only
        source directory during an in-place import) are logged instead of
        aborting the import, leaving the file in the state produced by the
        metadata write.

        Return whether the mtime could be applied.
        """
        try:
            self.write_file_mtime(util.syspath(path), mtime)
        except OSError as exc:
            self._log.warning(
                "Could not preserve mtime {} for '{}': {}",
                mtime,
                util.displayable_path(path),
                exc,
            )
            return False

        # Keep the item's `mtime` field in sync with the file on disk, but
        # only when the write actually targeted the item's own file.
        if path == item.path:
            item.mtime = int(mtime)
        return True

    def record_before_move(
        self, item: Item, source: bytes, destination: bytes
    ) -> None:
        """Capture the source mtime before a rename.

        This must happen before the move: for a cross-device move the
        source file is gone by the time `item_moved` fires.  The value is
        parked in `pending_mtime` and only committed once the move
        succeeds, so a failed rename leaves no stale timestamp behind.
        """
        mtime = os.stat(util.syspath(source)).st_mtime
        self.pending_mtime[destination] = mtime
        self._log.debug(
            "Pending mtime {} for '{}' (source '{}')",
            mtime,
            util.displayable_path(destination),
            util.displayable_path(source),
        )

    def record_after_move(
        self, item: Item, source: bytes, destination: bytes
    ) -> None:
        """Commit the mtime recorded before a successful rename."""
        mtime = self.pending_mtime.pop(destination, None)
        if mtime is not None:
            self.item_mtime[destination] = mtime
            self._log.debug(
                "Recorded mtime {} for moved item '{}'",
                mtime,
                util.displayable_path(destination),
            )

    def record_import_mtime(
        self, item: Item, source: bytes, destination: bytes
    ) -> None:
        """Record the file mtime of an item's source after it was
        copied/linked to its final destination.

        The event fires after the file operation succeeded and carries the
        actual destination (which may have been made unique to avoid
        overwriting an existing file), so the recorded timestamp always
        belongs to the file the item ends up pointing at.
        """
        mtime = os.stat(util.syspath(source)).st_mtime
        self.item_mtime[destination] = mtime
        self._log.debug(
            "Recorded mtime {} for item '{}' imported from '{}'",
            mtime,
            util.displayable_path(destination),
            util.displayable_path(source),
        )

    def update_album_times(self, lib: Library, album: Album) -> None:
        if self.reimported_album(album):
            self._log.debug(
                "Album '{.filepath}' is reimported, skipping import of "
                "added dates for the album and its items.",
                album,
            )
            # Drop the recorded mtimes so they cannot leak into later
            # writes of these files (the plugin instance survives across
            # tasks within one import session).
            for item in album.items():
                self.item_mtime.pop(item.path, None)
            return

        album_mtimes = []
        for item in album.items():
            mtime = self.item_mtime.pop(item.path, None)
            if mtime:
                album_mtimes.append(mtime)
                if self.config["preserve_mtimes"].get(bool):
                    self.apply_file_mtime(item, item.path, mtime)
                    item.store()
        album.added = min(album_mtimes)
        self._log.debug(
            "Import of album '{0.album}', selected album.added={0.added} "
            "from item file mtimes.",
            album,
        )
        album.store()

    def update_item_times(self, lib: Library, item: Item) -> None:
        if self.reimported_item(item):
            self._log.debug(
                "Item '{.filepath}' is reimported, skipping import of added date.",
                item,
            )
            self.item_mtime.pop(item.path, None)
            return
        mtime = self.item_mtime.pop(item.path, None)
        if mtime:
            item.added = mtime
            if self.config["preserve_mtimes"].get(bool):
                self.apply_file_mtime(item, item.path, mtime)
            self._log.debug(
                "Import of item '{0.filepath}', selected item.added={0.added}",
                item,
            )
            item.store()

    def update_after_write_time(self, item: Item, path: bytes) -> None:
        """Restore the mtime of the written file after a metadata write.

        During an import the write targets the file at its final location
        in the library directory; the source file's original mtime was
        recorded there and is written back to *that* file.  The source
        file itself is never touched (for in-place imports the source and
        destination are the same file, so its pre-import mtime is simply
        restored).  Outside of an import (e.g. ``beet write``), fall back
        to the item's stored `added` value.
        """
        if not self.config["preserve_write_mtimes"].get(bool):
            return

        # Prefer the mtime recorded for this import; `path` identifies
        # the file that was actually written.
        mtime = self.item_mtime.get(path)
        if mtime is None and path != item.path:
            mtime = self.item_mtime.get(item.path)
        if mtime is None:
            mtime = item.added

        if mtime:
            self.apply_file_mtime(item, path, mtime)
            self._log.debug(
                "Write of item '{0.filepath}', preserved mtime={1}",
                item,
                mtime,
            )
