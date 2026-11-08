"""Playlist panel: step through several saved projects in the normal video + 3D workspace.

Each item opens through MainWindow.load_project, exactly like File > Open Project, so model switching,
people, previews and teardown stay on their existing paths. The playlist itself (items, current index,
auto-advance) is app state; it is saved only to a user-chosen playlist file and the last file/auto-advance
choice to config/local.json, never into a project. Opening a project still records its view (frame, trim,
camera) the same way File > Open Project does. Inference is never started from here.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDockWidget, QFileDialog, QGridLayout, QHBoxLayout,
                               QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from . import playlist as pl

READY_POLL_MS = 100
READY_TIMEOUT_MS = 15000
PENDING_TIMEOUT_MS = 120000


class PlaylistWorkspace:
    def init_playlist(self):
        self.playlist_items = []
        self.playlist_index = None
        self.playlist_file = None
        self._playlist_autoplay = False
        self._playlist_wait = QTimer(self)
        self._playlist_wait.setInterval(READY_POLL_MS)
        self._playlist_wait.timeout.connect(self._playlist_poll_ready)
        self._playlist_waited = 0
        self._playlist_pending = None
        self._playlist_pending_waited = 0
        self._playlist_retry = QTimer(self)
        self._playlist_retry.setInterval(250)
        self._playlist_retry.timeout.connect(self._playlist_retry_pending)

        dock = QDockWidget('Playlist', self)
        dock.setObjectName('playlistDock')
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        body = QWidget(dock)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        nav = QHBoxLayout()
        self.playlist_prev_button = QPushButton('◀ Prev', body)
        self.playlist_next_button = QPushButton('Next ▶', body)
        self.playlist_prev_button.setToolTip('Open the previous project in the playlist and play it (Alt+Left).')
        self.playlist_next_button.setToolTip('Open the next project in the playlist and play it (Alt+Right).')
        self.playlist_prev_button.clicked.connect(lambda: self.playlist_step(-1))
        self.playlist_next_button.clicked.connect(lambda: self.playlist_step(1))
        nav.addWidget(self.playlist_prev_button)
        nav.addWidget(self.playlist_next_button)
        lay.addLayout(nav)
        self.playlist_auto = QCheckBox('Auto-advance at clip end', body)
        self.playlist_auto.setToolTip('When playback reaches the end of the clip range, open and play the next project. Off: playback loops as usual.')
        lay.addWidget(self.playlist_auto)

        self.playlist_list = QListWidget(body)
        self.playlist_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.playlist_list.setToolTip('Double-click a project to open and play it.')
        self.playlist_list.itemDoubleClicked.connect(lambda item: self.playlist_play(self.playlist_list.row(item)))
        lay.addWidget(self.playlist_list, 1)

        grid = QGridLayout()
        buttons = [
            ('Add Current', self.playlist_add_current, 'Add the open project (and its current model) to the playlist.'),
            ('Add Projects...', self.playlist_add_projects, 'Choose one or more project.json files to add.'),
            ('Add Folder...', self.playlist_add_folder, 'Add every project found in a folder (two levels deep), for example the projects folder.'),
            ('Remove', self.playlist_remove, 'Remove the selected item from the playlist. The project itself is not changed.'),
            ('Move Up', lambda: self.playlist_move(-1), 'Move the selected item up.'),
            ('Move Down', lambda: self.playlist_move(1), 'Move the selected item down.'),
            ('Open...', self.playlist_open_file, 'Open a saved playlist file.'),
            ('Save As...', self.playlist_save_file, 'Save this playlist to a file. Paths next to or below the file are stored relative to it.'),
        ]
        self.playlist_edit_buttons = []
        for i, (text, slot, tip) in enumerate(buttons):
            b = QPushButton(text, body)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            grid.addWidget(b, i // 2, i % 2)
            self.playlist_edit_buttons.append(b)
        lay.addLayout(grid)
        dock.setWidget(body)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        dock.hide()
        self.playlist_dock = dock

        view_menu = self.menuBar().addMenu('View')
        view_menu.setToolTipsVisible(True)
        toggle = dock.toggleViewAction()
        toggle.setText('Playlist')
        toggle.setShortcut(QKeySequence('Ctrl+L'))
        toggle.setToolTip('Show or hide the Playlist panel for reviewing several projects in turn.')
        view_menu.addAction(toggle)
        self.playlist_prev_action = view_menu.addAction('Previous Playlist Item', lambda: self.playlist_step(-1))
        self.playlist_prev_action.setShortcut(QKeySequence('Alt+Left'))
        self.playlist_next_action = view_menu.addAction('Next Playlist Item', lambda: self.playlist_step(1))
        self.playlist_next_action.setShortcut(QKeySequence('Alt+Right'))
        # Move View between File and Help.
        bar = self.menuBar()
        actions = bar.actions()
        help_action = next((a for a in actions if a.text() == 'Help'), None)
        if help_action is not None:
            bar.removeAction(view_menu.menuAction())
            bar.insertMenu(help_action, view_menu)

        try:
            from .resource_safety import path as settings_path, read_settings
            file, auto = pl.remembered(read_settings(settings_path()))
        except (OSError, ValueError):
            file, auto = None, True
        self.playlist_auto.setChecked(auto)
        self.playlist_auto.toggled.connect(lambda _: self._playlist_remember())
        if file and Path(file).exists():
            try:
                self.playlist_load_file(Path(file), show=False)
            except (OSError, ValueError):
                pass
        self.sync_playlist_controls()

    # ----- state helpers
    def playlist_can_switch(self):
        return not self.busy and not self.contact_saving and not getattr(self, '_pear_active', False)

    def sync_playlist_controls(self):
        if not hasattr(self, 'playlist_dock'):
            return
        can = not getattr(self, '_pear_active', False)  # while a task runs, a switch waits for it (see playlist_play)
        n = len(self.playlist_items)
        for w in (self.playlist_prev_button, self.playlist_next_button, self.playlist_prev_action, self.playlist_next_action):
            w.setEnabled(can and n > 0)
        self.playlist_list.setEnabled(can)
        for b in self.playlist_edit_buttons:
            b.setEnabled(not self.busy)
        if self.playlist_edit_buttons:
            self.playlist_edit_buttons[0].setEnabled(not self.busy and self.project is not None)

    def _playlist_refresh(self):
        self.playlist_list.clear()
        for i, item in enumerate(self.playlist_items):
            exists = (pl.target(item) / 'project.json').exists()
            row = QListWidgetItem(('▶ ' if i == self.playlist_index else '    ') + pl.describe(item) + ('' if exists else '  (missing)'))
            row.setToolTip(pl.target(item).as_posix())
            if i == self.playlist_index:
                f = QFont(row.font())
                f.setBold(True)
                row.setFont(f)
            if not exists:
                row.setForeground(Qt.gray)
            self.playlist_list.addItem(row)
        if self.playlist_index is not None and 0 <= self.playlist_index < self.playlist_list.count():
            self.playlist_list.setCurrentRow(self.playlist_index)
        self.playlist_dock.setWindowTitle('Playlist' + (f' ({len(self.playlist_items)})' if self.playlist_items else '')
                                          + (' - ' + Path(self.playlist_file).name if self.playlist_file else ''))
        self.sync_playlist_controls()

    def _playlist_remember(self):
        try:
            pl.remember(self.playlist_file, self.playlist_auto.isChecked())
        except (OSError, ValueError) as error:
            self.status.setText('Playlist preference not saved: ' + str(error))

    def _playlist_add(self, items):
        room = pl.MAX_ITEMS - len(self.playlist_items)
        self.playlist_items.extend(items[:max(0, room)])
        self._playlist_refresh()
        self.playlist_dock.show()
        if len(items) > room:
            self.status.setText(f'Playlist is limited to {pl.MAX_ITEMS} items.')

    # ----- editing
    def playlist_add_current(self):
        if self.project is None:
            return
        from .project_workspace import root_for
        try:
            root = root_for(self.project) or Path(self.project)
        except (OSError, ValueError):
            root = Path(self.project)
        model = Path(self.project).name if Path(self.project) != Path(root) and Path(self.project).name in pl.MODELS else None
        self._playlist_add([pl.make_item(Path(root).resolve(), model)])

    def playlist_add_projects(self):
        from .paths import PROJECTS
        names, _ = QFileDialog.getOpenFileNames(self, 'Add Projects to Playlist', str(PROJECTS), 'Project (project.json)')
        items = []
        for name in names:
            folder = Path(name).parent
            model = folder.name if folder.name in pl.MODELS and (folder.parent / 'project.json').exists() else None
            items.append(pl.make_item((folder.parent if model else folder).resolve(), model))
        if items:
            self._playlist_add(items)

    def playlist_add_folder(self):
        from .paths import PROJECTS
        folder = QFileDialog.getExistingDirectory(self, 'Add Projects in Folder', str(PROJECTS))
        if not folder:
            return
        items = pl.discover(Path(folder))
        if not items:
            self.status.setText('No RGB MoCap projects found in ' + folder)
            return
        self._playlist_add(items)
        self.status.setText(f'Added {len(items)} project(s) to the playlist.')

    def playlist_remove(self):
        row = self.playlist_list.currentRow()
        if not 0 <= row < len(self.playlist_items):
            return
        del self.playlist_items[row]
        if self.playlist_index is not None:
            if row == self.playlist_index:
                self.playlist_index = None
            elif row < self.playlist_index:
                self.playlist_index -= 1
        self._playlist_refresh()

    def playlist_move(self, step):
        row = self.playlist_list.currentRow()
        new = row + step
        if not (0 <= row < len(self.playlist_items) and 0 <= new < len(self.playlist_items)):
            return
        items = self.playlist_items
        items[row], items[new] = items[new], items[row]
        if self.playlist_index == row:
            self.playlist_index = new
        elif self.playlist_index == new:
            self.playlist_index = row
        self._playlist_refresh()
        self.playlist_list.setCurrentRow(new)

    def playlist_open_file(self):
        name, _ = QFileDialog.getOpenFileName(self, 'Open Playlist', str(Path(self.playlist_file).parent if self.playlist_file else Path.home()),
                                              'RGB MoCap playlist (*.json)')
        if name:
            try:
                self.playlist_load_file(Path(name))
            except (OSError, ValueError) as error:
                self.status.setText('Cannot open playlist: ' + str(error))

    def playlist_load_file(self, path, show=True):
        path = Path(path).resolve()
        items = pl.resolve_items(pl.load(path), path.parent)
        self.playlist_items = items
        self.playlist_index = None
        self.playlist_file = str(path)
        self._playlist_refresh()
        if show:
            self.playlist_dock.show()
            self._playlist_remember()
            self.status.setText(f'Playlist loaded: {len(items)} project(s). Double-click one, or press Alt+Right to start.')

    def playlist_save_file(self):
        name, _ = QFileDialog.getSaveFileName(self, 'Save Playlist As', self.playlist_file or str(Path.home() / 'playlist.json'),
                                              'RGB MoCap playlist (*.json)')
        if not name:
            return
        try:
            pl.save(Path(name), self.playlist_items)
            self.playlist_file = str(Path(name).resolve())
            self._playlist_refresh()
            self._playlist_remember()
            self.status.setText('Playlist saved: ' + name)
        except (OSError, ValueError) as error:
            self.status.setText('Cannot save playlist: ' + str(error))

    # ----- playback
    def playlist_step(self, step):
        nxt = pl.neighbour(self.playlist_index, len(self.playlist_items), step)
        if nxt is None:
            self.status.setText('End of playlist.' if step > 0 else 'Start of playlist.')
            return
        self.playlist_play(nxt)

    def playlist_play(self, index, autoplay=True):
        if not 0 <= index < len(self.playlist_items):
            return False
        if getattr(self, '_pear_active', False):
            self.status.setText('Leave PEAR before switching playlist items.')
            return False
        if self.busy or self.contact_saving:
            # A short task (for example preview preparation after opening a project) is running:
            # open this item as soon as it finishes instead of dropping the request.
            self._playlist_pending = (index, autoplay)
            self._playlist_pending_waited = 0
            self._playlist_retry.start()
            self.status.setText(f'Playlist: opening item {index + 1} when the current task finishes...')
            return None
        self._playlist_retry.stop()
        self._playlist_pending = None
        item = self.playlist_items[index]
        folder = pl.target(item)
        if not (folder / 'project.json').exists():
            self.status.setText('Playlist item is missing: ' + folder.as_posix())
            return False
        self._playlist_wait.stop()
        self.stop_playback()
        try:
            ok = self.load_project(folder)
        except (OSError, ValueError, KeyError) as error:
            self.status.setText('Cannot open playlist item: ' + str(error))
            return False
        if ok is False:
            return False
        self.playlist_index = index
        self._playlist_refresh()
        self.status.setText(f'Playlist {index + 1}/{len(self.playlist_items)}: {pl.describe(item)}')
        if autoplay:
            self._playlist_waited = 0
            self._playlist_wait.start()
        return True

    def _playlist_poll_ready(self):
        self._playlist_waited += READY_POLL_MS
        if self.timer.isActive():
            self._playlist_wait.stop()
            return
        if self.info is not None and self.video_readable and self.play_button.isEnabled() and self.preview_data is not None:
            self._playlist_wait.stop()
            self.slider.setValue(self.slider.clip_in)
            self.toggle_play()
        elif self._playlist_waited >= READY_TIMEOUT_MS:
            self._playlist_wait.stop()
            if self.info is not None and self.video_readable and self.play_button.isEnabled():
                self.slider.setValue(self.slider.clip_in)
                self.toggle_play()  # play the video even if no motion preview is available

    def playback_end_reached(self):
        """Called by next_frame when the playhead passes the clip end. True stops looping and advances."""
        if not (self.playlist_index is not None and self.playlist_auto.isChecked() and self.playlist_items):
            return False
        nxt = pl.neighbour(self.playlist_index, len(self.playlist_items), 1)
        self.stop_playback()
        self.slider.setValue(max(self.slider.clip_in, self.slider.clip_out - 1))
        if nxt is None:
            self.status.setText('End of playlist.')
        else:
            QTimer.singleShot(0, lambda: self.playlist_play(nxt))
        return True

    def _playlist_retry_pending(self):
        if self._playlist_pending is None:
            self._playlist_retry.stop()
            return
        self._playlist_pending_waited += self._playlist_retry.interval()
        if getattr(self, '_pear_active', False) or self._playlist_pending_waited > PENDING_TIMEOUT_MS:
            self._playlist_retry.stop()
            self._playlist_pending = None
            self.status.setText('Playlist: the current task is still running; press Next again when it finishes.')
            return
        if not (self.busy or self.contact_saving):
            index, autoplay = self._playlist_pending
            self.playlist_play(index, autoplay)

    def shutdown_playlist(self):
        self._playlist_wait.stop()
        self._playlist_retry.stop()
        self._playlist_pending = None
