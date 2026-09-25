from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import fnmatch
import os
import posixpath
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import traceback

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, Signal, QSize, QEvent, QTimer
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence, QPalette, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QTabBar, QTableWidget, QTableWidgetItem, QToolBar, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from backend import Site, Entry, FS, LocalFS, SSHFS, connect, copy_tree, Cancelled
from paths import load_config, save_config
from terminal import TerminalWindow


PROTOCOLS = ["SFTP", "SCP", "FTP", "FTPS", "FTPS Implicit", "WebDAV HTTPS", "WebDAV HTTP", "S3"]
KEYRING_NAME = "BridgeCommander sites"
ICON_DIR = Path(__file__).resolve().parent / "assets" / "icons"


def silk(name):
    return QIcon(str(ICON_DIR / f"{name}.png"))


def vault():
    import keyring
    return keyring


def human_size(size):
    if size < 1024:
        return f"{size} Б"
    value = float(size)
    for unit in ("КБ", "МБ", "ГБ", "ТБ"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} ПБ"


def join_path(base, name, local):
    return os.path.join(base, name) if local else posixpath.join(base, name)


class JobSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object, object)
    trust = Signal(str, str, str, object)
    question = Signal(str, object)


class Job(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = JobSignals()
        self.cancelled = threading.Event()

    def report(self, current, total):
        if self.cancelled.is_set():
            raise Cancelled("Операция отменена")
        self.signals.progress.emit(int(current), int(total))

    def _ask(self, signal, *args):
        if self.cancelled.is_set():
            raise Cancelled("Операция отменена")
        holder = {"event": threading.Event(), "yes": False}
        signal.emit(*args, holder)
        while not holder["event"].wait(0.1):
            if self.cancelled.is_set():
                raise Cancelled("Операция отменена")
        return holder["yes"]

    def trust(self, host, algorithm, fingerprint):
        return self._ask(self.signals.trust, host, algorithm, fingerprint)

    def question(self, message):
        return self._ask(self.signals.question, message)

    def run(self):
        try:
            if self.cancelled.is_set():
                raise Cancelled("Операция отменена")
            result = self.fn(self)
            if self.cancelled.is_set():
                if isinstance(result, FS):
                    result.close()
                raise Cancelled("Операция отменена")
            self.signals.finished.emit(result)
        except Cancelled as error:
            self.signals.failed.emit(str(error))
        except Exception as error:
            self.signals.failed.emit(f"{type(error).__name__}: {error}")


class FileTree(QTreeWidget):
    activated = Signal()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.activated.emit()


class FileItem(QTreeWidgetItem):
    def __lt__(self, other):
        mine = self.data(0, Qt.ItemDataRole.UserRole)
        theirs = other.data(0, Qt.ItemDataRole.UserRole)
        column = self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column == 1:
            return Path(mine.name).suffix.casefold() < Path(theirs.name).suffix.casefold()
        if column == 2:
            return mine.size < theirs.size
        if column == 4:
            return mine.mtime < theirs.mtime
        if column == 5:
            return mine.mode < theirs.mode
        return mine.name.casefold() < theirs.name.casefold()


class FilePane(QWidget):
    navigate = Signal(str)
    open_file = Signal(object)
    activated = Signal()
    context_requested = Signal(object)
    bookmark_requested = Signal()
    new_folder_requested = Signal()
    filter_requested = Signal()

    def __init__(self, title, local=False):
        super().__init__()
        self.local = local
        self.path = str(Path.home()) if local else "/"
        self.entries = []
        self.visible_count = 0
        self.show_hidden = False
        self.filter_text = ""
        self.history = []
        self.history_index = -1
        self.history_navigation = False
        self.title = QLabel("Macintosh HD" if local else "No session")
        self.title.setObjectName("paneTitle")
        self.path_edit = QLineEdit(self.path)
        self.path_edit.setFixedHeight(22)
        self.path_edit.returnPressed.connect(lambda: self.navigate.emit(self.path_edit.text()))
        self.tree = FileTree()
        self.tree.setHeaderLabels(["Name", "Ext", "Size", "Type", "Changed", "Rights"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context)
        self.tree.itemDoubleClicked.connect(self._double_click)
        self.tree.activated.connect(self.activated)
        self.tree.itemSelectionChanged.connect(self._update_status)
        self.status = QLabel("Нет файлов")
        self.status.setObjectName("paneStatus")
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Фильтр: *.txt")
        self.filter_edit.textChanged.connect(self._filter)
        self.filter_edit.setVisible(False)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(1)
        locations = QToolButton()
        locations.setIcon(silk("drive" if local else "server"))
        locations.setToolTip("Быстрый переход к диску или домашней папке" if local else "Быстрый переход к корню сервера")
        locations.setFixedSize(24, 23)
        locations.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        places = QMenu(locations)
        if local:
            places.addAction("Macintosh HD  /", lambda: self.navigate.emit("/"))
            places.addAction("Домашняя папка", self.home)
            places.addAction("Внешние диски  /Volumes", lambda: self.navigate.emit("/Volumes"))
        else:
            places.addAction("Корень сервера  /", self.home)
            places.addAction("Текущая папка", self.refresh)
        locations.setMenu(places)
        head.addWidget(locations)
        head.addWidget(self.title, 1)
        navigation = [
            ("arrow_left", "Назад", self.back),
            ("arrow_right", "Вперёд", self.forward),
            ("arrow_up", "На уровень выше", self.up),
            ("house", "Домашняя папка / корень", self.home),
            ("arrow_refresh", "Обновить", self.refresh),
            ("folder_star", "Добавить папку в закладки", lambda: self.bookmark_requested.emit()),
            ("folder_add", "Создать папку", lambda: self.new_folder_requested.emit()),
            ("find", "Фильтр файлов", lambda: self.filter_requested.emit()),
        ]
        self.navigation_buttons = []
        for icon_name, hint, handler in navigation:
            button = QToolButton()
            button.setIcon(silk(icon_name))
            button.setIconSize(QSize(16, 16))
            button.setToolTip(hint)
            button.setFixedSize(23, 23)
            button.clicked.connect(handler)
            head.addWidget(button)
            self.navigation_buttons.append(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        layout.addLayout(head)
        layout.addWidget(self.path_edit)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.status)
        self.tree.header().setStretchLastSection(False)
        for index, width in [(0, 205), (1, 43), (2, 68), (3, 87), (4, 135), (5, 72)]:
            self.tree.header().setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
            self.tree.header().resizeSection(index, width)

    def _double_click(self, item, _column):
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry.is_dir:
            self.navigate.emit(entry.path)
        else:
            self.open_file.emit(entry)

    def _context(self, point):
        self.activated.emit()
        self.context_requested.emit(self.tree.viewport().mapToGlobal(point))

    def up(self):
        parent = (str(Path(self.path).parent) if self.local else posixpath.dirname(self.path.rstrip("/")) or "/")
        self.navigate.emit(parent)

    def home(self):
        self.navigate.emit(str(Path.home()) if self.local else "/")

    def refresh(self):
        self.navigate.emit(self.path)

    def back(self):
        if self.history_index > 0:
            self.history_index -= 1
            self.history_navigation = True
            self.navigate.emit(self.history[self.history_index])

    def forward(self):
        if self.history_index + 1 < len(self.history):
            self.history_index += 1
            self.history_navigation = True
            self.navigate.emit(self.history[self.history_index])

    def selected(self):
        return [entry for item in self.tree.selectedItems()
                if (entry := item.data(0, Qt.ItemDataRole.UserRole)) and entry.name != ".."]

    def focused(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def set_entries(self, path, entries):
        if self.history_navigation:
            self.history_navigation = False
        elif not self.history or self.history[self.history_index] != path:
            self.history = self.history[:self.history_index + 1] + [path]
            self.history_index = len(self.history) - 1
        self.path = path
        self.path_edit.setText(path)
        self.entries = entries
        self._render()

    def _filter(self, text):
        self.filter_text = text.strip()
        self._render()

    def _render(self):
        self.tree.setUpdatesEnabled(False)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        icon_dir = silk("folder")
        icon_file = silk("page_white")
        count = 0
        parent = str(Path(self.path).parent) if self.local else posixpath.dirname(self.path.rstrip("/")) or "/"
        if parent != self.path:
            entry = Entry("..", parent, True)
            item = FileItem(["..", "", "", "File folder", "", ""])
            item.setIcon(0, icon_dir)
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            self.tree.addTopLevelItem(item)
        for entry in self.entries:
            if not self.show_hidden and entry.name.startswith("."):
                continue
            if self.filter_text and not fnmatch.fnmatch(entry.name.lower(), self.filter_text.lower()):
                continue
            date = datetime.fromtimestamp(entry.mtime).strftime("%d/%m/%Y %H:%M") if entry.mtime else ""
            ext = "" if entry.is_dir else Path(entry.name).suffix[1:].lower()
            kind = "File folder" if entry.is_dir else "File"
            perms = stat.filemode(entry.mode)[1:] if entry.mode else ""
            item = FileItem([entry.name, ext, "" if entry.is_dir else f"{entry.size:,}",
                             kind, date, perms])
            item.setIcon(0, icon_dir if entry.is_dir else icon_file)
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            self.tree.addTopLevelItem(item)
            count += 1
        self.visible_count = count
        self.tree.setSortingEnabled(True)
        self.tree.setUpdatesEnabled(True)
        self._update_status()

    def _update_status(self):
        selected = self.selected()
        total = sum(e.size for e in selected if not e.is_dir)
        self.status.setText(f"{self.visible_count} объектов · выбрано {len(selected)} · {human_size(total)}")


class AdvancedSiteDialog(QDialog):
    def __init__(self, parent, site):
        super().__init__(parent)
        self.setWindowTitle("Advanced Site Settings")
        self.resize(470, 260)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.key = QLineEdit(site.key_file)
        key_holder = QWidget()
        key_layout = QHBoxLayout(key_holder)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.key)
        browse = QPushButton("…")
        browse.setFixedWidth(26)
        browse.clicked.connect(self._browse_key)
        key_layout.addWidget(browse)
        self.remote = QLineEdit(site.remote_path)
        self.local = QLineEdit(site.local_path)
        self.region = QLineEdit(site.region)
        self.token = QLineEdit(site.session_token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        for title, control in [("Private key file", key_holder), ("Remote directory", self.remote),
                               ("Local directory", self.local), ("S3 region", self.region),
                               ("S3 session token", self.token)]:
            form.addRow(title + ":", control)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_key(self):
        filename, _ = QFileDialog.getOpenFileName(self, "SSH private key", str(Path.home() / ".ssh"))
        if filename:
            self.key.setText(filename)

    def apply(self, site):
        site.key_file = self.key.text().strip()
        site.remote_path = self.remote.text().strip() or "/"
        site.local_path = self.local.text().strip()
        site.region = self.region.text().strip()
        site.session_token = self.token.text()


class SiteDialog(QDialog):
    GROUPS = ["My Workspace", "Private", "University", "Work"]

    def __init__(self, parent=None, site=None, saved=None, groups=None,
                 on_save=None, on_delete=None, on_rename=None, on_group=None):
        super().__init__(parent)
        self.setWindowTitle("Login")
        self.resize(625, 275)
        self.setMinimumSize(600, 260)
        self.saved = saved if saved is not None else []
        self.groups = groups if groups is not None else list(self.GROUPS)
        self.on_save = on_save
        self.on_delete = on_delete
        self.on_rename = on_rename
        self.on_group = on_group
        self.site = site or Site()
        self.selected_group = "My Workspace"

        whole = QVBoxLayout(self)
        whole.setContentsMargins(6, 6, 6, 6)
        whole.setSpacing(5)
        content = QHBoxLayout()
        content.setSpacing(6)
        self.sites = QTreeWidget()
        self.sites.setHeaderHidden(True)
        self.sites.setFixedWidth(190)
        self.sites.setRootIsDecorated(True)
        self.sites.setAlternatingRowColors(False)
        self.sites.itemSelectionChanged.connect(self._choose)
        content.addWidget(self.sites)

        right = QVBoxLayout()
        box = QGroupBox("Session")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        grid.setContentsMargins(9, 12, 9, 8)
        self.protocol = QComboBox()
        self.protocol.addItems(PROTOCOLS)
        self.protocol.currentTextChanged.connect(self._set_default_port)
        self.protocol.setFixedWidth(155)
        self.host = QLineEdit()
        self.host.setPlaceholderText("example.com")
        self.port = QLineEdit()
        self.port.setFixedWidth(65)
        self.user = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        grid.addWidget(QLabel("File protocol:"), 0, 0, 1, 2)
        grid.addWidget(self.protocol, 1, 0, 1, 2)
        grid.addWidget(QLabel("Hostname:"), 2, 0)
        grid.addWidget(QLabel("Port number:"), 2, 1)
        grid.addWidget(self.host, 3, 0)
        grid.addWidget(self.port, 3, 1)
        grid.addWidget(QLabel("Username:"), 4, 0)
        grid.addWidget(QLabel("Password:"), 4, 1)
        grid.addWidget(self.user, 5, 0)
        grid.addWidget(self.password, 5, 1)
        grid.setColumnStretch(0, 1)
        tools = QHBoxLayout()
        save_button = QPushButton("Save")
        save_button.clicked.connect(self._save)
        tools.addWidget(save_button)
        tools.addStretch(1)
        advanced_button = QPushButton("Advanced…")
        advanced_button.clicked.connect(self._advanced)
        tools.addWidget(advanced_button)
        grid.addLayout(tools, 6, 0, 1, 2)
        right.addWidget(box)
        right.addStretch(1)
        content.addLayout(right, 1)
        whole.addLayout(content, 1)

        footer = QHBoxLayout()
        tools_button = QPushButton("Tools ▾")
        tools_menu = QMenu(tools_button)
        tools_menu.addAction("Paste session URL…", self._paste_url)
        tools_menu.addAction("New site", self._new_site)
        tools_button.setMenu(tools_menu)
        footer.addWidget(tools_button)
        manage_button = QPushButton("Manage ▾")
        manage_menu = QMenu(manage_button)
        manage_menu.addAction("New folder…", self._new_group)
        manage_menu.addAction("Rename site…", self._rename)
        manage_menu.addAction("Delete site…", self._delete)
        manage_button.setMenu(manage_menu)
        footer.addWidget(manage_button)
        footer.addStretch(1)
        login = QPushButton("Login")
        login.setDefault(True)
        login.clicked.connect(self._accept)
        footer.addWidget(login)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        help_button = QPushButton("Help")
        help_button.clicked.connect(lambda: QMessageBox.information(
            self, "Login help", "Choose a protocol, enter the server address and credentials, then click Login. Save stores the site; passwords are stored in macOS Keychain."))
        footer.addWidget(help_button)
        whole.addLayout(footer)

        self._populate(self.site)
        self._rebuild_tree()

    def _set_default_port(self, protocol):
        ports = {"SFTP": 22, "SCP": 22, "FTP": 21, "FTPS": 21, "FTPS Implicit": 990,
                 "WebDAV HTTPS": 443, "WebDAV HTTP": 80, "S3": 443}
        self.port.setText(str(ports.get(protocol, 22)))

    def _rebuild_tree(self, selected_name=None):
        self.sites.blockSignals(True)
        self.sites.clear()
        new_item = QTreeWidgetItem(["New Site"])
        new_item.setData(0, Qt.ItemDataRole.UserRole, ("new", ""))
        new_item.setIcon(0, silk("server_add"))
        self.sites.addTopLevelItem(new_item)
        target = new_item
        groups = {name: None for name in self.GROUPS + list(self.groups)}
        for site in self.saved:
            groups.setdefault(site.get("group", "My Workspace"), None)
        for name in groups:
            folder = QTreeWidgetItem([name])
            folder.setData(0, Qt.ItemDataRole.UserRole, ("group", name))
            folder.setIcon(0, silk("folder"))
            self.sites.addTopLevelItem(folder)
            groups[name] = folder
        for saved_site in self.saved:
            folder = groups[saved_site.get("group", "My Workspace")]
            item = QTreeWidgetItem([saved_site["name"]])
            item.setData(0, Qt.ItemDataRole.UserRole, ("site", saved_site["name"]))
            item.setIcon(0, silk("computer"))
            folder.addChild(item)
            folder.setExpanded(True)
            if selected_name == saved_site["name"]:
                target = item
        self.sites.setCurrentItem(target)
        self.sites.blockSignals(False)

    def _choose(self):
        item = self.sites.currentItem()
        if item is None:
            return
        kind, value = item.data(0, Qt.ItemDataRole.UserRole)
        if kind == "group":
            self.selected_group = value
            self.site.group = value
            return
        if kind == "new":
            self._populate(Site(group=self.selected_group))
            return
        data = next((dict(s) for s in self.saved if s["name"] == value), None)
        if data is None:
            return
        data["password"] = vault().get_password(KEYRING_NAME, value + ":password") or ""
        data["session_token"] = vault().get_password(KEYRING_NAME, value + ":token") or ""
        self._populate(Site(**data))

    def _populate(self, site):
        self.site = site
        self.selected_group = site.group
        self.protocol.setCurrentText(site.protocol)
        self.host.setText(site.host)
        self.port.setText(str(site.port))
        self.user.setText(site.username)
        self.password.setText(site.password)

    def _collect(self, require_host=True):
        if require_host and self.protocol.currentText() != "S3" and not self.host.text().strip():
            QMessageBox.warning(self, "Hostname", "Enter the server address.")
            return None
        try:
            port = int(self.port.text())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "Port", "Enter a port from 1 to 65535.")
            return None
        name = self.site.name
        if name == "Новое подключение":
            name = self.host.text().strip() or "S3"
        return Site(name=name, group=self.site.group or self.selected_group,
                    protocol=self.protocol.currentText(), host=self.host.text().strip(), port=port,
                    username=self.user.text().strip(), password=self.password.text(),
                    key_file=self.site.key_file, remote_path=self.site.remote_path or "/",
                    local_path=self.site.local_path, region=self.site.region,
                    session_token=self.site.session_token)

    def _advanced(self):
        current = self._collect(require_host=False)
        if current is None:
            return
        dialog = AdvancedSiteDialog(self, current)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply(current)
            self.site = current

    def _save(self):
        site = self._collect()
        if site is None:
            return
        if not any(s["name"] == site.name for s in self.saved):
            name, ok = QInputDialog.getText(self, "Save site", "Site name:", text=site.name)
            if not ok or not name.strip():
                return
            site.name = name.strip()
        if self.on_save:
            self.on_save(site)
        self.site = site
        self._rebuild_tree(site.name)

    def _new_site(self):
        self.sites.setCurrentItem(self.sites.topLevelItem(0))
        self._populate(Site(group=self.selected_group))

    def _new_group(self):
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip() and name.strip() not in self.groups:
            if self.on_group:
                self.on_group(name.strip())
            self._rebuild_tree()

    def _selected_saved_name(self):
        item = self.sites.currentItem()
        if item is None:
            return None
        kind, value = item.data(0, Qt.ItemDataRole.UserRole)
        return value if kind == "site" else None

    def _rename(self):
        old_name = self._selected_saved_name()
        if not old_name:
            return
        name, ok = QInputDialog.getText(self, "Rename site", "New name:", text=old_name)
        if ok and name.strip() and name.strip() != old_name and self.on_rename:
            if not self.on_rename(old_name, name.strip()):
                return
            self.site.name = name.strip()
            self._rebuild_tree(name.strip())

    def _delete(self):
        name = self._selected_saved_name()
        if not name:
            return
        if QMessageBox.question(self, "Delete site", f"Delete saved site '{name}'?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            if self.on_delete:
                self.on_delete(name)
            self._new_site()
            self._rebuild_tree()

    def _paste_url(self):
        from urllib.parse import urlparse, unquote
        url, ok = QInputDialog.getText(self, "Session URL", "Paste SFTP, SCP, FTP, WebDAV or S3 URL:")
        if not ok or not url:
            return
        parsed = urlparse(url)
        mapping = {"sftp": "SFTP", "scp": "SCP", "ftp": "FTP", "ftps": "FTPS",
                   "http": "WebDAV HTTP", "https": "WebDAV HTTPS", "s3": "S3"}
        protocol = mapping.get(parsed.scheme.lower())
        if not protocol or not parsed.hostname:
            QMessageBox.warning(self, "Session URL", "Unsupported or invalid URL.")
            return
        defaults = {"SFTP": 22, "SCP": 22, "FTP": 21, "FTPS": 21,
                    "WebDAV HTTP": 80, "WebDAV HTTPS": 443, "S3": 443}
        if protocol == "S3":
            self._populate(Site(name=parsed.hostname, group=self.selected_group,
                                protocol="S3", host="", port=443,
                                remote_path="/" + parsed.hostname + unquote(parsed.path or "")))
            return
        self._populate(Site(name=parsed.hostname, group=self.selected_group,
                            protocol=protocol, host=parsed.hostname,
                            port=parsed.port or defaults[protocol],
                            username=unquote(parsed.username or ""),
                            password=unquote(parsed.password or ""),
                            remote_path=unquote(parsed.path or "/")))

    def _accept(self):
        site = self._collect()
        if site:
            self.site = site
            self.accept()


class TextEditor(QDialog):
    def __init__(self, parent, title, text, editable):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(850, 620)
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit()
        self.editor.setPlainText(text)
        self.editor.setReadOnly(not editable)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if editable:
            save = buttons.addButton("Сохранить", QDialogButtonBox.ButtonRole.AcceptRole)
            save.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class SyncDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Синхронизация папок")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Скопировать отсутствующие и изменённые файлы:"))
        self.direction = QComboBox()
        self.direction.addItems(["Локальная → сервер", "Сервер → локальная"])
        layout.addWidget(self.direction)
        self.subfolders = QCheckBox("Включать вложенные папки")
        self.subfolders.setChecked(True)
        layout.addWidget(self.subfolders)
        layout.addWidget(QLabel("Перед копированием будет показан список изменений."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class PreferencesDialog(QDialog):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.setWindowTitle("Настройки Bridge Commander")
        self.setFixedWidth(410)
        layout = QVBoxLayout(self)

        appearance = QGroupBox("Внешний вид")
        appearance_form = QFormLayout(appearance)
        self.theme = QComboBox()
        for title, value in [("Как в macOS", "system"), ("Светлая", "light"), ("Тёмная", "dark")]:
            self.theme.addItem(title, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(config.get("theme", "system"))))
        appearance_form.addRow("Тема:", self.theme)
        self.toolbar_labels = QCheckBox("Показывать подписи под значками")
        self.toolbar_labels.setChecked(config.get("toolbar_labels", False))
        appearance_form.addRow("", self.toolbar_labels)
        self.compact_rows = QCheckBox("Компактные строки файлов")
        self.compact_rows.setChecked(config.get("compact_rows", True))
        appearance_form.addRow("", self.compact_rows)
        layout.addWidget(appearance)

        files = QGroupBox("Файлы")
        files_form = QFormLayout(files)
        self.show_hidden = QCheckBox("Показывать скрытые файлы")
        self.show_hidden.setChecked(config.get("show_hidden", False))
        files_form.addRow("", self.show_hidden)
        self.confirm_delete = QCheckBox("Спрашивать перед удалением")
        self.confirm_delete.setChecked(config.get("confirm_delete", True))
        files_form.addRow("", self.confirm_delete)
        layout.addWidget(files)

        transfers = QGroupBox("Передача файлов")
        transfers_form = QFormLayout(transfers)
        self.overwrite = QComboBox()
        for title, value in [("Спрашивать при замене", "ask"),
                             ("Всегда заменять", "overwrite"),
                             ("Пропускать существующие", "skip")]:
            self.overwrite.addItem(title, value)
        self.overwrite.setCurrentIndex(max(0, self.overwrite.findData(config.get("overwrite", "ask"))))
        transfers_form.addRow("Если файл уже есть:", self.overwrite)
        layout.addWidget(transfers)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel |
                                   QDialogButtonBox.StandardButton.Apply)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(
            lambda: parent.apply_preferences(self.values()))
        layout.addWidget(buttons)

    def values(self):
        return {
            "theme": self.theme.currentData(),
            "toolbar_labels": self.toolbar_labels.isChecked(),
            "compact_rows": self.compact_rows.isChecked(),
            "show_hidden": self.show_hidden.isChecked(),
            "confirm_delete": self.confirm_delete.isChecked(),
            "overwrite": self.overwrite.currentData(),
        }


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bridge Commander")
        self.resize(880, 550)
        self.setMinimumSize(740, 450)
        self.config = load_config()
        self.local_fs = LocalFS()
        self.sessions = []
        self.terminal_windows = set()
        self.displayed_session = None
        self.active = "local"
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)  # Protocol clients are deliberately serial.
        self.pool.setExpiryTimeout(1000)
        self.jobs = {}
        self.job_index = 0
        self.local_generation = 0
        self.closing = False
        self.shutdown_ready = False
        self.shutdown_timer = None
        self.shutdown_deadline = None
        self.show_hidden = self.config.get("show_hidden", False)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(1)
        self.tabs = QTabBar()
        self.tabs.setTabsClosable(True)
        self.tabs.setExpanding(False)
        self.tabs.setFixedHeight(27)
        self.tabs.setVisible(False)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        layout.addWidget(self.tabs)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.local = FilePane("Local", True)
        self.remote = FilePane("Remote", False)
        self.local.show_hidden = self.show_hidden
        self.remote.show_hidden = self.show_hidden
        self.splitter.addWidget(self.local)
        self.splitter.addWidget(self.remote)
        self.splitter.setSizes([520, 520])
        layout.addWidget(self.splitter, 1)
        self.local.navigate.connect(self.navigate_local)
        self.remote.navigate.connect(self.navigate_remote)
        self.local.open_file.connect(lambda e: self.open_entry(e, self.local, False))
        self.remote.open_file.connect(lambda e: self.open_entry(e, self.remote, False))
        self.local.activated.connect(lambda: self.set_active("local"))
        self.remote.activated.connect(lambda: self.set_active("remote"))
        self.local.tree.installEventFilter(self)
        self.remote.tree.installEventFilter(self)
        self.local.context_requested.connect(lambda p: self._context_menu(p, "local"))
        self.remote.context_requested.connect(lambda p: self._context_menu(p, "remote"))
        for side, pane in (("local", self.local), ("remote", self.remote)):
            pane.bookmark_requested.connect(lambda s=side: self._pane_bookmark(s))
            pane.new_folder_requested.connect(lambda s=side: self._pane_new_folder(s))
            pane.filter_requested.connect(lambda s=side: self._pane_filter(s))

        self.queue = QTableWidget(0, 3)
        self.queue.setHorizontalHeaderLabels(["Задача", "Состояние", "Прогресс"])
        self.queue.horizontalHeader().setStretchLastSection(True)
        self.queue.setFixedHeight(145)
        self.queue.setVisible(False)
        self.queue.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue.customContextMenuRequested.connect(self._queue_menu)
        layout.addWidget(self.queue)
        self.command_row = QWidget()
        command_layout = QHBoxLayout(self.command_row)
        command_layout.setContentsMargins(4, 0, 4, 0)
        self.prompt = QLabel(">")
        self.command = QLineEdit()
        self.command.setPlaceholderText("Командная строка: локальная или SSH")
        self.command.returnPressed.connect(self.run_command)
        command_layout.addWidget(self.prompt)
        command_layout.addWidget(self.command, 1)
        self.command_row.setVisible(False)
        layout.addWidget(self.command_row)
        function_row = QHBoxLayout()
        function_row.setSpacing(0)
        for label, icon, fn in [("F2 Rename", "pencil", self.rename),
                                ("F3 View", "eye", self.view),
                                ("F4 Edit", "page_white", self.edit),
                                ("F5 Copy", "page_copy", self.copy),
                                ("F6 Move", "arrow_switch", self.move_files),
                                ("F7 New folder", "folder_add", self.new_folder),
                                ("F8 Delete", "delete", self.delete),
                                ("F9 Properties", "cog", self.properties),
                                ("F10 Quit", "disconnect", self.close)]:
            button = QPushButton(label)
            button.setFlat(True)
            button.setFixedHeight(28)
            button.setIcon(silk(icon))
            button.setIconSize(QSize(16, 16))
            button.clicked.connect(fn)
            function_row.addWidget(button)
        layout.addLayout(function_row)
        self.setCentralWidget(root)
        self._build_actions()
        self._build_toolbar()
        self.statusBar().showMessage("Ready")
        local_path = self.config.get("local_path", str(Path.home()))
        self.navigate_local(local_path if Path(local_path).is_dir() else str(Path.home()))

    def _action(self, menu, text, fn, shortcut=None):
        action = QAction(text, self)
        action.triggered.connect(fn)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        menu.addAction(action)
        return action

    def _build_actions(self):
        bar = self.menuBar()
        bar.setNativeMenuBar(False)
        local = bar.addMenu("Local")
        self._action(local, "Open directory…", lambda: self.local.path_edit.setFocus())
        self._action(local, "Parent directory", lambda: self.local.up())
        self._action(local, "Home directory", lambda: self.local.home())
        self._action(local, "Refresh", lambda: self.local.refresh())

        mark = bar.addMenu("Mark")
        self._action(mark, "Select all", self.select_all, "Ctrl+A")
        self._action(mark, "Clear selection", self.clear_selection)
        self._action(mark, "Invert selection", self.invert_selection)

        files = bar.addMenu("Files")
        self._action(files, "Rename", self.rename, "F2")
        self._action(files, "View", self.view, "F3")
        self._action(files, "Edit", self.edit, "F4")
        self._action(files, "Copy", self.copy, "F5")
        self._action(files, "Move", self.move_files, "F6")
        self._action(files, "Create directory", self.new_folder, "F7")
        self._action(files, "Delete", self.delete, "F8")
        self._action(files, "Properties", self.properties, "F9")

        commands = bar.addMenu("Commands")
        self._action(commands, "Compare directories", self.compare)
        self._action(commands, "Synchronize…", self.synchronize, "Ctrl+S")
        self._action(commands, "Find files…", self.find_files, "Ctrl+F")
        self._action(commands, "Refresh", self.refresh, "Ctrl+R")
        self._action(commands, "SSH terminal…", self.open_terminal, "Ctrl+T")

        session = bar.addMenu("Session")
        self._action(session, "New session…", self.new_session, "Ctrl+N")
        self._action(session, "Save session", self.save_current_site)
        self._action(session, "Disconnect", self.disconnect)
        self._action(session, "Close tab", self.disconnect, "Ctrl+W")
        self._action(session, "Quit", self.close, "F10")

        options = bar.addMenu("Options")
        self._action(options, "Настройки…", self.open_preferences, "Ctrl+,")
        theme_menu = options.addMenu("Тема")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self.theme_actions = {}
        for label, value in [("Как в macOS", "system"), ("Светлая", "light"), ("Тёмная", "dark")]:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(value == self.config.get("theme", "system"))
            action.triggered.connect(lambda _checked=False, selected=value:
                                     self.apply_preferences({"theme": selected}))
            theme_group.addAction(action)
            theme_menu.addAction(action)
            self.theme_actions[value] = action
        options.addSeparator()
        self.hidden_action = self._action(options, "Show hidden files", self.toggle_hidden, "Ctrl+Shift+H")
        self.hidden_action.setCheckable(True)
        self.hidden_action.setChecked(self.show_hidden)
        self._action(options, "File filter", self.toggle_filter, "Ctrl+Shift+F")
        self.queue_action = self._action(options, "Transfer queue", self.toggle_queue, "Ctrl+Q")
        self.queue_action.setCheckable(True)
        self._action(options, "Cancel selected transfer", self.cancel_selected_job)
        self.command_action = self._action(options, "Command line", self.toggle_command)
        self.command_action.setCheckable(True)
        self.bookmark_menu = options.addMenu("Bookmarks")
        self._refresh_bookmark_menu()

        remote = bar.addMenu("Remote")
        self._action(remote, "Open directory…", lambda: self.remote.path_edit.setFocus())
        self._action(remote, "Parent directory", lambda: self.remote.up())
        self._action(remote, "Root directory", lambda: self.remote.home())
        self._action(remote, "Refresh", lambda: self.remote.refresh())

        self._action(commands, "Parent directory", self.up, "Alt+Up")
        self._action(commands, "Home directory", self.home, "Ctrl+H")
        self._action(commands, "Back", lambda: self.pane().back(), "Alt+Left")
        self._action(commands, "Forward", lambda: self.pane().forward(), "Alt+Right")

        help_menu = bar.addMenu("Help")
        self._action(help_menu, "About", self.about)

    def _build_toolbar(self):
        toolbar = QToolBar("Основные команды")
        toolbar.setObjectName("mainToolbar")
        toolbar.setIconSize(QSize(16, 16))
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        commands = [
            ("Открыть окно подключения", "world", self.new_session),
            ("Подключиться", "server_add", self.new_session),
            ("Отключиться", "disconnect", self.disconnect),
            ("Сохранить сеанс", "disk", self.save_current_site),
            ("Обновить обе панели", "arrow_refresh", self.refresh_both),
            ("Назад", "arrow_left", lambda: self.pane().back()),
            ("Вперёд", "arrow_right", lambda: self.pane().forward()),
            ("На уровень выше", "arrow_up", self.up),
            ("Домашняя папка / корень", "house", self.home),
            ("Выделить всё", "table_multiple", self.select_all),
            ("Снять выделение", "table_delete", self.clear_selection),
            ("Копировать", "page_copy", self.copy),
            ("Переместить", "arrow_switch", self.move_files),
            ("Создать папку", "folder_add", self.new_folder),
            ("Удалить", "delete", self.delete),
            ("Синхронизировать", "arrow_inout", self.synchronize),
            ("Найти файлы", "find", self.find_files),
            ("Открыть SSH-терминал", "application_osx_terminal", self.open_terminal),
        ]
        for index, (label, icon_name, callback) in enumerate(commands):
            if index in (1, 5, 9, 11, 15):
                toolbar.addSeparator()
            action = toolbar.addAction(silk(icon_name), label)
            action.setToolTip(label)
            action.setStatusTip(label)
            action.triggered.connect(callback)
        toolbar.addSeparator()
        self.transfer_preset = QComboBox()
        self.transfer_preset.setObjectName("transferPreset")
        for label, value in [("Default", "ask"), ("Overwrite", "overwrite"),
                             ("Skip existing", "skip")]:
            self.transfer_preset.addItem(label, value)
        self.transfer_preset.setCurrentIndex(max(0, self.transfer_preset.findData(
            self.config.get("overwrite", "ask"))))
        self.transfer_preset.setToolTip("Правило для уже существующих файлов")
        self.transfer_preset.currentIndexChanged.connect(self._preset_changed)
        toolbar.addWidget(self.transfer_preset)
        self.toolbar = toolbar
        self._apply_toolbar_mode()

    def _apply_toolbar_mode(self):
        labels = self.config.get("toolbar_labels", False)
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon if labels
                                        else Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.toolbar.setFixedHeight(51 if labels else 29)

    def _preset_changed(self):
        self.config["overwrite"] = self.transfer_preset.currentData()
        save_config(self.config)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Tab:
            if watched is self.local.tree:
                self.remote.tree.setFocus()
            elif watched is self.remote.tree:
                self.local.tree.setFocus()
            return True
        return super().eventFilter(watched, event)

    def set_active(self, side):
        self.active = side
        self.prompt.setText(">" if side == "local" else "$")
        self.local.title.setProperty("active", side == "local")
        self.remote.title.setProperty("active", side == "remote")
        for title in (self.local.title, self.remote.title):
            title.style().unpolish(title)
            title.style().polish(title)

    def _pane_bookmark(self, side):
        self.set_active(side)
        self.add_bookmark()

    def _pane_new_folder(self, side):
        self.set_active(side)
        self.new_folder()

    def _pane_filter(self, side):
        self.set_active(side)
        self.toggle_filter()

    def open_preferences(self):
        dialog = PreferencesDialog(self, self.config)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.apply_preferences(dialog.values())

    def apply_preferences(self, values):
        settings = {"theme": "system", "toolbar_labels": False, "compact_rows": True,
                    "show_hidden": False, "confirm_delete": True, "overwrite": "ask"}
        settings.update(self.config)
        settings.update(values)
        self.config.update(settings)
        self.show_hidden = settings["show_hidden"]
        self.local.show_hidden = self.show_hidden
        self.remote.show_hidden = self.show_hidden
        self.local._render()
        self.remote._render()
        self.hidden_action.setChecked(self.show_hidden)
        self.transfer_preset.blockSignals(True)
        self.transfer_preset.setCurrentIndex(max(0, self.transfer_preset.findData(settings["overwrite"])))
        self.transfer_preset.blockSignals(False)
        self._apply_toolbar_mode()
        self.theme_actions[settings["theme"]].setChecked(True)
        configure_app(QApplication.instance(), settings["theme"], settings["compact_rows"])
        save_config(self.config)

    def pane(self):
        return self.local if self.active == "local" else self.remote

    def other_pane(self):
        return self.remote if self.active == "local" else self.local

    def select_all(self):
        tree = self.pane().tree
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if entry and entry.name != "..":
                item.setSelected(True)

    def clear_selection(self):
        self.pane().tree.clearSelection()

    def invert_selection(self):
        tree = self.pane().tree
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if entry and entry.name != "..":
                item.setSelected(not item.isSelected())

    def current_session(self):
        index = self.tabs.currentIndex()
        return self.sessions[index] if 0 <= index < len(self.sessions) else None

    def _require_session(self):
        session = self.current_session()
        if not session:
            QMessageBox.information(self, "Нет подключения", "Сначала подключись к серверу.")
        return session

    def _trust_prompt(self, host, algorithm, fingerprint, holder):
        if self.closing:
            holder["event"].set()
            return
        answer = QMessageBox.question(self, "Новый ключ SSH-сервера",
            f"Сервер: {host}\nАлгоритм: {algorithm}\nОтпечаток: {fingerprint}\n\n"
            "Сверь отпечаток с администратором сервера. Доверять этому ключу?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        holder["yes"] = answer == QMessageBox.StandardButton.Yes
        holder["event"].set()

    def _question_prompt(self, message, holder):
        if self.closing:
            holder["event"].set()
            return
        answer = QMessageBox.question(self, "Подтверждение", message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        holder["yes"] = answer == QMessageBox.StandardButton.Yes
        holder["event"].set()

    def submit(self, title, fn, done=None, show_error=True):
        if self.closing:
            return None
        if not self.jobs and self.queue.rowCount() > 160:
            for _ in range(self.queue.rowCount() - 100):
                self.queue.removeRow(0)
        self.job_index += 1
        job_id = self.job_index
        row = self.queue.rowCount()
        self.queue.insertRow(row)
        self.queue.setItem(row, 0, QTableWidgetItem(title))
        self.queue.item(row, 0).setData(Qt.ItemDataRole.UserRole, job_id)
        self.queue.setItem(row, 1, QTableWidgetItem("В очереди"))
        bar = QProgressBar()
        bar.setRange(0, 0)
        self.queue.setCellWidget(row, 2, bar)
        job = Job(fn)
        self.jobs[job_id] = job

        def progress(current, total):
            if self.closing:
                return
            self.queue.item(row, 1).setText("Выполняется")
            if total > 0:
                bar.setRange(0, 100)
                bar.setValue(min(100, int(current * 100 / total)))
            else:
                bar.setRange(0, 0)

        def success(result):
            self.jobs.pop(job_id, None)
            if self.closing:
                return
            self.queue.item(row, 1).setText("Готово")
            bar.setRange(0, 100)
            bar.setValue(100)
            self.statusBar().showMessage(f"Готово: {title}", 7000)
            if done:
                done(result)

        def failure(error):
            self.jobs.pop(job_id, None)
            if self.closing:
                return
            cancelled = error == "Операция отменена"
            self.queue.item(row, 1).setText("Отменено" if cancelled else "Ошибка")
            bar.setRange(0, 100)
            bar.setValue(0)
            self.statusBar().showMessage(f"{'Отменено' if cancelled else 'Ошибка'}: {error}", 12000)
            if show_error and not cancelled:
                QMessageBox.warning(self, "Не удалось выполнить", f"{title}\n\n{error}")

        job.signals.progress.connect(progress)
        job.signals.finished.connect(success)
        job.signals.failed.connect(failure)
        job.signals.trust.connect(self._trust_prompt)
        job.signals.question.connect(self._question_prompt)
        self.pool.start(job)
        return job

    def _queue_menu(self, point):
        menu = QMenu(self)
        menu.addAction("Отменить задачу", self.cancel_selected_job)
        menu.exec(self.queue.viewport().mapToGlobal(point))

    def cancel_selected_job(self):
        row = self.queue.currentRow()
        if row < 0:
            return
        item = self.queue.item(row, 0)
        job = self.jobs.get(item.data(Qt.ItemDataRole.UserRole)) if item else None
        if job:
            job.cancelled.set()
            self.queue.item(row, 1).setText("Отмена…")

    def new_session(self):
        dialog = SiteDialog(self, saved=self.config.setdefault("sites", []),
                            groups=self.config.setdefault("groups", list(SiteDialog.GROUPS)),
                            on_save=self._save_site, on_delete=self._delete_site,
                            on_rename=self._rename_site, on_group=self._add_group)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        site = dialog.site

        def finished(fs):
            self.sessions.append({"site": site, "fs": fs, "path": site.remote_path or "/"})
            index = self.tabs.addTab(site.name)
            self.tabs.setVisible(True)
            self.tabs.setCurrentIndex(index)
            if site.local_path and Path(site.local_path).is_dir():
                self.navigate_local(site.local_path)

        self.submit(f"Подключение к {site.host or 'S3'}", lambda job: connect(site, job.trust), finished)

    def _save_site(self, site):
        sites = self.config.setdefault("sites", [])
        sites[:] = [s for s in sites if s["name"] != site.name]
        sites.append(site.public_dict())
        save_config(self.config)
        if site.password:
            vault().set_password(KEYRING_NAME, site.name + ":password", site.password)
        if site.session_token:
            vault().set_password(KEYRING_NAME, site.name + ":token", site.session_token)

    def _delete_site(self, name):
        sites = self.config.setdefault("sites", [])
        sites[:] = [site for site in sites if site["name"] != name]
        save_config(self.config)
        for kind in ("password", "token"):
            try:
                vault().delete_password(KEYRING_NAME, name + ":" + kind)
            except Exception:
                pass

    def _rename_site(self, old_name, new_name):
        sites = self.config.setdefault("sites", [])
        if any(site["name"] == new_name for site in sites):
            QMessageBox.warning(self, "Rename site", "A site with this name already exists.")
            return False
        for site in sites:
            if site["name"] == old_name:
                site["name"] = new_name
        save_config(self.config)
        for kind in ("password", "token"):
            secret = vault().get_password(KEYRING_NAME, old_name + ":" + kind)
            if secret:
                vault().set_password(KEYRING_NAME, new_name + ":" + kind, secret)
                try:
                    vault().delete_password(KEYRING_NAME, old_name + ":" + kind)
                except Exception:
                    pass
        return True

    def _add_group(self, name):
        groups = self.config.setdefault("groups", list(SiteDialog.GROUPS))
        if name not in groups:
            groups.append(name)
            save_config(self.config)

    def save_current_site(self):
        session = self._require_session()
        if session:
            session["site"].remote_path = self.remote.path
            session["site"].local_path = self.local.path
            self._save_site(session["site"])
            self.statusBar().showMessage("Подключение сохранено", 5000)

    def _tab_changed(self, index):
        if self.displayed_session is not None:
            self.displayed_session["history"] = list(self.remote.history)
            self.displayed_session["history_index"] = self.remote.history_index
        if not 0 <= index < len(self.sessions):
            self.displayed_session = None
            self.remote.history = []
            self.remote.history_index = -1
            self.remote.set_entries("/", [])
            self.remote.title.setText("No session")
            return
        session = self.sessions[index]
        self.displayed_session = session
        self.remote.history = list(session.get("history", []))
        self.remote.history_index = session.get("history_index", -1)
        self.remote.history_navigation = False
        self.remote.title.setText(session["site"].name)
        self.navigate_remote(session["path"])

    def _close_tab(self, index):
        if not 0 <= index < len(self.sessions):
            return
        session = self.sessions.pop(index)
        for terminal in tuple(self.terminal_windows):
            if terminal.session is session:
                terminal.close()
        self.tabs.removeTab(index)
        if not self.sessions:
            self.tabs.setVisible(False)
        self.submit(f"Отключение от {session['site'].name}", lambda _job: session["fs"].close(), show_error=False)

    def disconnect(self):
        self._close_tab(self.tabs.currentIndex())

    def open_terminal(self):
        session = self._require_session()
        if not session:
            return
        if not isinstance(session["fs"], SSHFS):
            QMessageBox.information(self, "SSH-терминал",
                                    "Интерактивный терминал доступен для SFTP и SCP по SSH.")
            return
        terminal = TerminalWindow(session, self.remote.path, self)
        terminal.closed.connect(self.terminal_windows.discard)
        self.terminal_windows.add(terminal)
        terminal.show()

    def navigate_local(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        self.local_generation += 1
        generation = self.local_generation
        self.statusBar().showMessage(f"Чтение локальной папки {path}…")

        def finished(entries):
            if generation != self.local_generation:
                return
            self.local.set_entries(path, entries)
            self.config["local_path"] = path

        self.submit(f"Чтение локальной папки {path}",
                    lambda job: self.local_fs.listdir(path, job.cancelled), finished)

    def navigate_remote(self, path):
        session = self.current_session()
        if not session:
            return
        path = posixpath.normpath("/" + path.lstrip("/"))
        fs = session["fs"]
        self.statusBar().showMessage(f"Чтение папки сервера {path}…")

        def finished(entries):
            session["path"] = path
            if self.current_session() is session:
                self.remote.set_entries(path, entries)

        self.submit(f"Чтение {path}", lambda _job: fs.listdir(path), finished)

    def refresh(self):
        self.pane().refresh()

    def refresh_both(self):
        self.navigate_local(self.local.path)
        if self.current_session():
            self.navigate_remote(self.remote.path)

    def up(self):
        self.pane().up()

    def home(self):
        self.pane().home()

    def _selected_or_warn(self):
        entries = self.pane().selected()
        if not entries:
            QMessageBox.information(self, "Выбери файл", "Выбери один или несколько файлов или папок.")
        return entries

    def _fs_for(self, pane):
        if pane.local:
            return self.local_fs
        session = self._require_session()
        return session["fs"] if session else None

    def transfer(self, move=False):
        entries = self._selected_or_warn()
        if not entries:
            return
        session = self._require_session()
        if not session:
            return
        source_pane, target_pane = self.pane(), self.other_pane()
        source_fs = self.local_fs if source_pane.local else session["fs"]
        target_fs = self.local_fs if target_pane.local else session["fs"]
        operation = "Перемещение" if move else "Копирование"
        overwrite = self.config.get("overwrite", "ask")

        def task(job):
            for entry in entries:
                target = join_path(target_pane.path, entry.name, target_pane.local)
                decide = (lambda _path: True) if overwrite == "overwrite" else (
                    (lambda _path: False) if overwrite == "skip" else
                    (lambda path: job.question(f"Файл {path} уже существует. Заменить его?")))
                copied = copy_tree(source_fs, target_fs, entry.path, target, entry.is_dir,
                                   job.report, decide)
                if move and copied:
                    source_fs.delete(entry.path, entry.is_dir)

        self.submit(f"{operation}: {len(entries)} объектов", task, lambda _: self.refresh_both())

    def copy(self):
        self.transfer(False)

    def move_files(self):
        self.transfer(True)

    def rename(self):
        entries = self._selected_or_warn()
        if len(entries) != 1:
            return
        entry = entries[0]
        pane = self.pane()
        name, ok = QInputDialog.getText(self, "Переименовать", "Новое имя:", text=entry.name)
        if not ok or not name or name == entry.name:
            return
        if "/" in name or name in (".", ".."):
            QMessageBox.warning(self, "Имя", "Имя не должно содержать /.")
            return
        fs = self._fs_for(pane)
        if not fs:
            return
        target = join_path(pane.path, name, pane.local)
        self.submit(f"Переименование {entry.name}",
                    lambda _job: fs.rename(entry.path, target), lambda _: pane.refresh())

    def new_folder(self):
        pane = self.pane()
        fs = self._fs_for(pane)
        if not fs:
            return
        name, ok = QInputDialog.getText(self, "Создать папку", "Имя папки:")
        if not ok or not name:
            return
        if "/" in name or name in (".", ".."):
            QMessageBox.warning(self, "Имя", "Имя не должно содержать /.")
            return
        path = join_path(pane.path, name, pane.local)
        self.submit(f"Создание папки {name}", lambda _job: fs.mkdir(path), lambda _: pane.refresh())

    def delete(self):
        entries = self._selected_or_warn()
        if not entries:
            return
        if self.config.get("confirm_delete", True):
            answer = QMessageBox.question(self, "Удаление",
                f"Удалить выбранные объекты ({len(entries)})? Папки будут удалены со всем содержимым.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        pane = self.pane()
        fs = self._fs_for(pane)
        if not fs:
            return
        def task(job):
            for i, entry in enumerate(entries):
                job.report(i, len(entries))
                fs.delete(entry.path, entry.is_dir)
            job.report(len(entries), len(entries))
        self.submit(f"Удаление {len(entries)} объектов", task, lambda _: pane.refresh())

    def properties(self):
        entries = self._selected_or_warn()
        if len(entries) != 1:
            return
        entry = entries[0]
        message = (f"Имя: {entry.name}\nПуть: {entry.path}\n"
                   f"Тип: {'Папка' if entry.is_dir else 'Файл'}\n"
                   f"Размер: {human_size(entry.size)} ({entry.size} байт)\n"
                   f"Изменён: {datetime.fromtimestamp(entry.mtime).isoformat(sep=' ', timespec='seconds') if entry.mtime else 'неизвестно'}\n"
                   f"Права: {format(entry.mode & 0o7777, '04o') if entry.mode else 'неизвестно'}")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Свойства")
        dialog.setText(message)
        change = None
        if entry.mode:
            change = dialog.addButton("Изменить права…", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        if change and dialog.clickedButton() is change:
            fs = self._fs_for(self.pane())
            if not fs:
                return
            value, ok = QInputDialog.getText(self, "Права", "Восьмеричные права (например 0755):",
                                              text=format(entry.mode & 0o7777, "04o"))
            if ok:
                try:
                    mode = int(value, 8)
                    if mode > 0o7777:
                        raise ValueError
                except ValueError:
                    QMessageBox.warning(self, "Права", "Введи число от 0000 до 7777.")
                    return
                pane = self.pane()
                self.submit("Изменение прав", lambda _job: fs.chmod(entry.path, mode),
                            lambda _: pane.refresh())

    def open_entry(self, entry, pane, editable):
        if entry.is_dir:
            pane.navigate.emit(entry.path)
            return
        if entry.size > 8 * 1024 * 1024:
            QMessageBox.information(self, "Большой файл", "Встроенный редактор открывает файлы до 8 МБ.")
            return
        fs = self._fs_for(pane)
        if not fs:
            return
        if pane.local:
            try:
                data = Path(entry.path).read_bytes()
            except OSError as error:
                QMessageBox.warning(self, "Открытие файла", str(error))
                return
            self._show_editor(entry, pane, data, editable)
            return
        # A unique temporary file avoids mixing simultaneously opened documents.
        temp_dir = tempfile.mkdtemp(prefix="bridge-commander-")
        temp_path = os.path.join(temp_dir, entry.name)

        def finished(_):
            pending_upload = self._show_editor(entry, pane, Path(temp_path).read_bytes(), editable, temp_path)
            if not pending_upload:
                shutil.rmtree(temp_dir, ignore_errors=True)
        self.submit(f"Открытие {entry.name}",
                    lambda job: fs.download(entry.path, temp_path, job.report), finished)

    def _show_editor(self, entry, pane, data, editable, temp_path=None):
        if b"\0" in data[:4096]:
            QMessageBox.information(self, "Двоичный файл", "Этот файл нельзя открыть как текст.")
            return False
        try:
            text = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = data.decode("cp1251", errors="replace")
            encoding = "cp1251"
        dialog = TextEditor(self, entry.name, text, editable)
        if dialog.exec() != QDialog.DialogCode.Accepted or not editable:
            return False
        new_data = dialog.editor.toPlainText().encode(encoding)
        if pane.local:
            temp_name = None
            try:
                fd, temp_name = tempfile.mkstemp(prefix=".bridge-edit-", dir=str(Path(entry.path).parent))
                with os.fdopen(fd, "wb") as file:
                    file.write(new_data)
                os.chmod(temp_name, entry.mode & 0o7777)
                os.replace(temp_name, entry.path)
                pane.refresh()
            except OSError as error:
                QMessageBox.warning(self, "Сохранение", str(error))
            finally:
                if temp_name and os.path.exists(temp_name):
                    os.unlink(temp_name)
            return False
        Path(temp_path).write_bytes(new_data)
        fs = self._fs_for(pane)
        if fs:
            self.submit(f"Сохранение {entry.name}",
                        lambda job: fs.upload(temp_path, entry.path, job.report),
                        lambda _: (pane.refresh(), shutil.rmtree(str(Path(temp_path).parent), ignore_errors=True)))
            return True
        return False

    def view(self):
        entries = self._selected_or_warn()
        if len(entries) == 1:
            self.open_entry(entries[0], self.pane(), False)

    def edit(self):
        entries = self._selected_or_warn()
        if len(entries) == 1:
            self.open_entry(entries[0], self.pane(), True)

    def compare(self):
        if not self._require_session():
            return
        local = {e.name: e for e in self.local.entries}
        remote = {e.name: e for e in self.remote.entries}
        local_only = set(local) - set(remote)
        remote_only = set(remote) - set(local)
        different = {name for name in set(local) & set(remote)
                     if local[name].is_dir != remote[name].is_dir or
                     (not local[name].is_dir and local[name].size != remote[name].size)}
        for pane, names in [(self.local, local_only | different),
                            (self.remote, remote_only | different)]:
            pane.tree.clearSelection()
            for i in range(pane.tree.topLevelItemCount()):
                item = pane.tree.topLevelItem(i)
                if item.data(0, Qt.ItemDataRole.UserRole).name in names:
                    item.setSelected(True)
        QMessageBox.information(self, "Сравнение папок",
            f"Только локально: {len(local_only)}\nТолько на сервере: {len(remote_only)}\nРазный размер или тип: {len(different)}\n\nОтличающиеся файлы выделены в панелях.")

    def synchronize(self):
        session = self._require_session()
        if not session:
            return
        dialog = SyncDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        upload = dialog.direction.currentIndex() == 0
        recursive = dialog.subfolders.isChecked()
        source_fs = self.local_fs if upload else session["fs"]
        target_fs = session["fs"] if upload else self.local_fs
        source_path = self.local.path if upload else self.remote.path
        target_path = self.remote.path if upload else self.local.path
        source_local = upload
        target_local = not upload

        def plan(job):
            changes = []
            def walk(src, dst):
                target = {e.name: e for e in target_fs.listdir(dst)}
                for e in source_fs.listdir(src):
                    job.report(len(changes), 0)
                    dest = join_path(dst, e.name, target_local)
                    other = target.get(e.name)
                    if e.is_dir:
                        if recursive:
                            if other and other.is_dir:
                                walk(e.path, dest)
                            else:
                                changes.append((e.path, dest, True, "Новая папка"))
                    elif other is None or other.is_dir or e.size != other.size or (e.mtime and other.mtime and e.mtime > other.mtime + 2):
                        changes.append((e.path, dest, False, "Новый/изменённый файл"))
            walk(source_path, target_path)
            return changes

        def planned(changes):
            if not changes:
                QMessageBox.information(self, "Синхронизация", "Различий для копирования нет.")
                return
            preview = "\n".join(f"{kind}: {src}" for src, _, _, kind in changes[:25])
            if len(changes) > 25:
                preview += f"\n… и ещё {len(changes) - 25}"
            answer = QMessageBox.question(self, "Синхронизация",
                f"Будет скопировано {len(changes)} объектов:\n\n{preview}\n\nПродолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            def execute(job):
                for index, (src, dst, is_dir, _) in enumerate(changes):
                    job.report(index, len(changes))
                    copy_tree(source_fs, target_fs, src, dst, is_dir)
                job.report(len(changes), len(changes))
            self.submit(f"Синхронизация {len(changes)} объектов", execute,
                        lambda _: self.refresh_both())

        self.submit("Анализ синхронизации", plan, planned)

    def find_files(self):
        pane = self.pane()
        fs = self._fs_for(pane)
        if not fs:
            return
        pattern, ok = QInputDialog.getText(self, "Поиск файлов", "Имя или маска (* и ?):", text="*")
        if not ok or not pattern:
            return
        def task(job):
            results = []
            def walk(path, depth):
                if depth > 20 or len(results) >= 1000:
                    return
                for e in fs.listdir(path):
                    job.report(len(results), 0)
                    if fnmatch.fnmatch(e.name.lower(), pattern.lower()):
                        results.append(e)
                    if e.is_dir and len(results) < 1000:
                        walk(e.path, depth + 1)
            walk(pane.path, 0)
            return results
        def finished(results):
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Результаты поиска: {pattern}")
            dialog.resize(700, 500)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel(f"Найдено: {len(results)}"))
            tree = QTreeWidget()
            tree.setHeaderLabels(["Имя", "Путь", "Размер"])
            for e in results:
                item = QTreeWidgetItem([e.name, e.path, human_size(e.size)])
                item.setData(0, Qt.ItemDataRole.UserRole, e)
                tree.addTopLevelItem(item)
            tree.header().resizeSection(0, 200)
            tree.header().resizeSection(1, 390)
            tree.itemDoubleClicked.connect(lambda item, _col: (dialog.accept(),
                pane.navigate.emit(posixpath.dirname(item.data(0, Qt.ItemDataRole.UserRole).path))))
            layout.addWidget(tree)
            close = QPushButton("Закрыть")
            close.clicked.connect(dialog.accept)
            layout.addWidget(close)
            dialog.exec()
        self.submit(f"Поиск {pattern}", task, finished)

    def toggle_hidden(self):
        self.show_hidden = not self.show_hidden
        self.local.show_hidden = self.show_hidden
        self.remote.show_hidden = self.show_hidden
        self.local._render()
        self.remote._render()
        self.hidden_action.setChecked(self.show_hidden)
        self.config["show_hidden"] = self.show_hidden
        save_config(self.config)

    def toggle_filter(self):
        pane = self.pane()
        pane.filter_edit.setVisible(not pane.filter_edit.isVisible())
        if pane.filter_edit.isVisible():
            pane.filter_edit.setFocus()
        else:
            pane.filter_edit.clear()

    def toggle_queue(self):
        shown = not self.queue.isVisible()
        self.queue.setVisible(shown)
        self.queue_action.setChecked(shown)

    def toggle_command(self):
        shown = not self.command_row.isVisible()
        self.command_row.setVisible(shown)
        self.command_action.setChecked(shown)
        if shown:
            self.command.setFocus()

    def run_command(self):
        command = self.command.text().strip()
        if not command:
            return
        pane = self.pane()
        if pane.local:
            def task(_job):
                p = subprocess.run(command, cwd=pane.path, shell=True, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
                return f"Код выхода: {p.returncode}\n\n{p.stdout}"
        else:
            session = self._require_session()
            if not session:
                return
            fs = session["fs"]
            if not hasattr(fs, "_shell"):
                QMessageBox.information(self, "Командная строка", "Удалённые команды доступны только для SSH (SFTP/SCP).")
                return
            remote_command = "cd -- " + shlex.quote(pane.path) + " && " + command
            task = lambda _job: fs._shell(remote_command).decode("utf-8", "replace")
        def show(output):
            dialog = TextEditor(self, f"Команда: {command}", output, False)
            dialog.exec()
        self.submit(f"Команда: {command}", task, show)
        self.command.clear()

    def _refresh_bookmark_menu(self):
        self.bookmark_menu.clear()
        self._action(self.bookmark_menu, "Добавить текущую папку…", self.add_bookmark)
        self.bookmark_menu.addSeparator()
        for bookmark in self.config.get("bookmarks", []):
            side = bookmark["side"]
            path = bookmark["path"]
            action = self.bookmark_menu.addAction(bookmark["name"])
            action.triggered.connect(lambda _checked=False, s=side, p=path:
                                     self.navigate_local(p) if s == "local" else self.navigate_remote(p))

    def add_bookmark(self):
        pane = self.pane()
        name, ok = QInputDialog.getText(self, "Новая закладка", "Название:", text=Path(pane.path).name or pane.path)
        if ok and name:
            self.config.setdefault("bookmarks", []).append({"name": name, "path": pane.path, "side": self.active})
            save_config(self.config)
            self._refresh_bookmark_menu()

    def _context_menu(self, point, side):
        self.set_active(side)
        menu = QMenu(self)
        for label, fn in [("Открыть / Просмотр", self.view), ("Изменить", self.edit),
                          ("Копировать", self.copy), ("Переместить", self.move_files),
                          ("Переименовать", self.rename), ("Создать папку", self.new_folder),
                          ("Удалить", self.delete), ("Свойства", self.properties),
                          ("Обновить", self.refresh)]:
            menu.addAction(label, fn)
        menu.exec(point)

    def about(self):
        QMessageBox.about(self, "Bridge Commander",
            "Bridge Commander 0.1.1 для macOS\n\nДвухпанельный файловый клиент: SFTP, SCP, FTP, FTPS, WebDAV, S3.\n\n"
            "Значки: Silk Icons, Mark James (CC BY 2.5).\n"
            "Независимое приложение, не связанное с WinSCP.")

    def closeEvent(self, event):
        if self.shutdown_ready:
            event.accept()
            return
        if self.closing:
            event.ignore()
            return
        self.closing = True
        self.config["local_path"] = self.local.path
        save_config(self.config)
        for job in list(self.jobs.values()):
            job.cancelled.set()
        if self.jobs or self.pool.activeThreadCount():
            event.ignore()
            self.statusBar().showMessage("Завершение фоновых операций…")
            self.setEnabled(False)
            self.shutdown_timer = QTimer(self)
            self.shutdown_timer.timeout.connect(self._try_finish_shutdown)
            self.shutdown_timer.start(200)
            self.shutdown_deadline = QTimer(self)
            self.shutdown_deadline.setSingleShot(True)
            self.shutdown_deadline.timeout.connect(self._force_shutdown_if_stalled)
            self.shutdown_deadline.start(5000)
            return
        self._close_sessions()
        self.shutdown_ready = True
        event.accept()

    def _try_finish_shutdown(self):
        if self.jobs or self.pool.activeThreadCount():
            return
        self.shutdown_timer.stop()
        if self.shutdown_deadline:
            self.shutdown_deadline.stop()
        self._close_sessions()
        self.shutdown_ready = True
        self.close()

    def _force_shutdown_if_stalled(self):
        if self.closing and (self.jobs or self.pool.activeThreadCount()):
            os._exit(0)

    def _close_sessions(self):
        for terminal in tuple(self.terminal_windows):
            terminal.close()
        for session in self.sessions:
            try:
                session["fs"].close()
            except Exception:
                pass
        self.sessions.clear()


def configure_app(app, theme="system", compact_rows=True):
    app.setApplicationName("Bridge Commander")
    app.setOrganizationName("Bridge Commander")
    app.setStyle("Fusion")
    app.setFont(QFont("Helvetica Neue", 9))
    dark = theme == "dark" or (theme == "system" and
                               app.styleHints().colorScheme() == Qt.ColorScheme.Dark)
    colors = ({
        "window": "#292929", "text": "#ececec", "base": "#202020",
        "alternate": "#282828", "button": "#373737", "border": "#595959",
        "header": "#343434", "selection": "#5d594d", "selection_text": "#ffffff",
        "muted": "#b6b6b6", "hover": "#49463f", "active": "#ffffff",
        "placeholder": "#a4a4a4",
    } if dark else {
        "window": "#f2f2f0", "text": "#1c1c1c", "base": "#ffffff",
        "alternate": "#f9f9f8", "button": "#eeeeec", "border": "#b9b9b5",
        "header": "#e7e7e4", "selection": "#d5d3ca", "selection_text": "#171717",
        "muted": "#555555", "hover": "#e3e1da", "active": "#171717",
        "placeholder": "#737373",
    })
    palette = QPalette()
    for role, color in [
        (QPalette.ColorRole.Window, "window"),
        (QPalette.ColorRole.WindowText, "text"),
        (QPalette.ColorRole.Base, "base"),
        (QPalette.ColorRole.AlternateBase, "alternate"),
        (QPalette.ColorRole.Text, "text"),
        (QPalette.ColorRole.Button, "button"),
        (QPalette.ColorRole.ButtonText, "text"),
        (QPalette.ColorRole.Highlight, "selection"),
        (QPalette.ColorRole.HighlightedText, "selection_text"),
        (QPalette.ColorRole.PlaceholderText, "placeholder"),
        (QPalette.ColorRole.ToolTipBase, "button"),
        (QPalette.ColorRole.ToolTipText, "text"),
        (QPalette.ColorRole.Link, "text"),
    ]:
        palette.setColor(role, QColor(colors[color]))
    app.setPalette(palette)
    css = """
        QMainWindow, QDialog { background: @window@; color: @text@; }
        QMenuBar, QMenu { background: @window@; color: @text@; }
        QMenuBar::item:selected, QMenu::item:selected { background: @selection@; color: @selection_text@; }
        QToolBar { background: @button@; spacing: 1px; padding: 1px 2px; border-bottom: 1px solid @border@; }
        QToolBar::separator { background: @border@; width: 1px; margin: 4px 3px; }
        QToolButton { color: @text@; background: transparent; border: 0; padding: 1px; }
        QToolButton:hover { background: @hover@; }
        QPushButton { color: @text@; background: @button@; border: 1px solid @border@;
                      border-radius: 2px; padding: 2px 7px; min-height: 18px; }
        QPushButton:hover { background: @hover@; }
        QPushButton:flat { background: transparent; border: 0; padding: 2px 3px; }
        QPushButton:flat:hover { background: @hover@; }
        QTreeWidget, QTableWidget { color: @text@; background: @base@; alternate-background-color: @alternate@;
                                    border: 1px solid @border@; selection-background-color: @selection@;
                                    selection-color: @selection_text@; }
        QTreeWidget::item { height: @row_height@px; }
        QTreeWidget::item:selected { background: @selection@; color: @selection_text@; }
        QHeaderView::section { background: @header@; color: @text@; padding: 1px 4px;
                               border: 0; border-right: 1px solid @border@; border-bottom: 1px solid @border@; }
        QLabel { color: @text@; }
        QLabel#paneTitle { color: @text@; font-weight: 600; }
        QLabel#paneTitle[active="true"] { color: @active@; }
        QLabel#paneStatus { color: @muted@; font-size: 10px; }
        QLineEdit, QPlainTextEdit { color: @text@; background: @base@; border: 1px solid @border@;
                                   border-radius: 1px; padding: 1px 3px; }
        QSplitter::handle { background: @border@; width: 2px; }
        QGroupBox { border: 1px solid @border@; margin-top: 6px; padding-top: 6px; }
        QGroupBox::title { subcontrol-origin: margin; left: 7px; color: @text@; }
        QComboBox { color: @text@; background: @base@; border: 1px solid @border@; padding: 1px 4px; }
        QComboBox QAbstractItemView { color: @text@; background: @base@; selection-background-color: @selection@; }
        QStatusBar { background: @window@; color: @muted@; }
        QTabBar::tab { color: @text@; background: @button@; padding: 3px 12px; border: 1px solid @border@; }
        QTabBar::tab:selected { background: @base@; }
        QCheckBox { color: @text@; }
    """
    for key, color in colors.items():
        css = css.replace(f"@{key}@", color)
    css = css.replace("@row_height@", "17" if compact_rows else "22")
    app.setStyleSheet(css)


def launch():
    app = QApplication([])
    settings = load_config()
    configure_app(app, settings.get("theme", "system"), settings.get("compact_rows", True))
    window = MainWindow()
    window.show()
    return app.exec()
