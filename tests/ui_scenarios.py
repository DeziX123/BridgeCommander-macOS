"""Offscreen interaction and lifecycle checks for Bridge Commander."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox, QPushButton

from backend import LocalFS, Site
from ui import MainWindow, PreferencesDialog, SiteDialog, SyncDialog, TextEditor, configure_app


app = QApplication([])
configure_app(app, "light", True)
passed = []


def check(name, condition):
    assert condition, name
    passed.append(name)
    print(f"PASS {len(passed):02d} {name}", flush=True)


def wait_until(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def toolbar_click(window, label):
    action = next(a for a in window.toolbar.actions() if a.text() == label)
    button = window.toolbar.widgetForAction(action)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    app.processEvents()


def select(window, name):
    tree = window.local.tree
    tree.clearSelection()
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item.text(0) == name:
            tree.setCurrentItem(item)
            item.setSelected(True)
            window.set_active("local")
            return
    raise AssertionError(f"missing local item: {name}")


def close_modal(expected, callback=None):
    def close():
        dialogs = [w for w in app.topLevelWidgets() if isinstance(w, expected) and w.isVisible()]
        assert len(dialogs) == 1, f"{expected.__name__} did not open"
        if callback:
            callback(dialogs[0])
        else:
            dialogs[0].reject()
    QTimer.singleShot(30, close)


with TemporaryDirectory(prefix="bridge-ui-scenarios-") as root, \
     patch("ui.load_config") as load_config, patch("ui.save_config"):
    local_root = Path(root) / "local"
    remote_root = Path(root) / "remote"
    local_root.mkdir()
    remote_root.mkdir()
    (local_root / "A").mkdir()
    (local_root / "B").mkdir()
    (local_root / "hello.txt").write_text("hello", encoding="utf-8")
    (local_root / "move.txt").write_text("move", encoding="utf-8")
    (local_root / ".secret").write_text("hidden", encoding="utf-8")
    load_config.return_value = {"sites": [], "bookmarks": [], "show_hidden": False,
                                "local_path": str(local_root), "theme": "light"}
    window = MainWindow()
    window.show()
    check("launch and first local listing", wait_until(lambda: window.local.path == str(local_root)
                                                        and len(window.local.entries) >= 5))
    check("compact window", window.width() == 880 and window.height() == 550)
    check("eighteen toolbar commands", len([a for a in window.toolbar.actions()
                                             if not a.icon().isNull()]) == 18)
    check("terminal toolbar icon", not next(a for a in window.toolbar.actions()
          if a.text() == "Открыть SSH-терминал").icon().isNull())
    check("all F-key buttons have icons", all(not button.icon().isNull()
          for button in window.findChildren(QPushButton) if button.text().startswith("F")))
    check("eight navigation buttons per pane", len(window.local.navigation_buttons) == 8
          and len(window.remote.navigation_buttons) == 8)

    close_modal(SiteDialog)
    toolbar_click(window, "Открыть окно подключения")
    check("globe opens and closes Login", not any(isinstance(w, SiteDialog) and w.isVisible()
                                                   for w in app.topLevelWidgets()))
    for _ in range(25):
        close_modal(SiteDialog)
        toolbar_click(window, "Открыть окно подключения")
    check("25 repeated Login openings", not any(isinstance(w, SiteDialog) and w.isVisible()
                                                for w in app.topLevelWidgets()))
    close_modal(SiteDialog)
    toolbar_click(window, "Подключиться")
    check("new session button opens Login", not any(isinstance(w, SiteDialog) and w.isVisible()
                                                     for w in app.topLevelWidgets()))
    with patch.object(QMessageBox, "information") as info:
        toolbar_click(window, "Открыть SSH-терминал")
    check("terminal without session explains requirement", info.called and not window.terminal_windows)

    window.local.path_edit.setText(str(local_root / "A"))
    window.local.path_edit.returnPressed.emit()
    check("path field navigates", wait_until(lambda: window.local.path == str(local_root / "A")))
    QTest.mouseClick(window.local.navigation_buttons[2], Qt.MouseButton.LeftButton)
    check("up button", wait_until(lambda: window.local.path == str(local_root)))
    QTest.mouseClick(window.local.navigation_buttons[0], Qt.MouseButton.LeftButton)
    check("back button", wait_until(lambda: window.local.path == str(local_root / "A")))
    QTest.mouseClick(window.local.navigation_buttons[1], Qt.MouseButton.LeftButton)
    check("forward button", wait_until(lambda: window.local.path == str(local_root)))
    toolbar_click(window, "Назад")
    check("toolbar back", wait_until(lambda: window.local.path == str(local_root / "A")))
    toolbar_click(window, "Вперёд")
    check("toolbar forward", wait_until(lambda: window.local.path == str(local_root)))
    toolbar_click(window, "На уровень выше")
    check("toolbar up", wait_until(lambda: window.local.path == str(Path(root))))
    toolbar_click(window, "Домашняя папка / корень")
    check("toolbar home", wait_until(lambda: window.local.path == str(Path.home())))
    window.navigate_local(str(local_root / "A"))
    window.navigate_local(str(local_root / "B"))
    check("rapid navigation keeps latest folder", wait_until(lambda: window.local.path == str(local_root / "B")))
    window.navigate_local(str(local_root))
    check("return to test folder", wait_until(lambda: window.local.path == str(local_root)))
    toolbar_click(window, "Обновить обе панели")
    check("refresh button", wait_until(lambda: not window.jobs))

    window.set_active("local")
    toolbar_click(window, "Выделить всё")
    check("select all", len(window.local.selected()) == 4)
    toolbar_click(window, "Снять выделение")
    check("clear selection", not window.local.selected())
    window.invert_selection()
    check("invert selection", len(window.local.selected()) == 4)
    window.clear_selection()

    window.hidden_action.trigger()
    check("show hidden files", window.local.visible_count == 5)
    window.hidden_action.trigger()
    check("hide hidden files", window.local.visible_count == 4)
    QTest.mouseClick(window.local.navigation_buttons[7], Qt.MouseButton.LeftButton)
    window.local.filter_edit.setText("*.txt")
    check("file filter", window.local.visible_count == 2)
    QTest.mouseClick(window.local.navigation_buttons[7], Qt.MouseButton.LeftButton)
    check("filter closes", not window.local.filter_edit.isVisible()
          and window.local.visible_count == 4)

    window.queue_action.trigger()
    check("queue opens", window.queue.isVisible())
    window.queue_action.trigger()
    check("queue closes", not window.queue.isVisible())
    window.command_action.trigger()
    check("command line opens", window.command_row.isVisible())
    window.command_action.trigger()
    check("command line closes", not window.command_row.isVisible())

    def set_dark(dialog):
        dialog.theme.setCurrentIndex(dialog.theme.findData("dark"))
        dialog.compact_rows.setChecked(False)
        dialog.accept()
    close_modal(PreferencesDialog, set_dark)
    window.open_preferences()
    check("settings apply dark theme", window.config["theme"] == "dark")
    window.theme_actions["light"].trigger()
    check("quick theme switch", window.config["theme"] == "light")
    window.transfer_preset.setCurrentIndex(window.transfer_preset.findData("skip"))
    check("Default preset changes overwrite rule", window.config["overwrite"] == "skip")

    with patch.object(QInputDialog, "getText", return_value=("Made", True)):
        QTest.mouseClick(window.local.navigation_buttons[6], Qt.MouseButton.LeftButton)
    check("new folder button", wait_until(lambda: (local_root / "Made").is_dir()
          and not window.jobs))
    select(window, "Made")
    with patch.object(QInputDialog, "getText", return_value=("Renamed", True)):
        window.rename()
    check("rename button", wait_until(lambda: (local_root / "Renamed").is_dir()
          and not window.jobs))
    select(window, "Renamed")
    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        window.delete()
    check("delete button", wait_until(lambda: not (local_root / "Renamed").exists()
          and not window.jobs))

    select(window, "hello.txt")
    close_modal(QMessageBox)
    window.properties()
    check("properties button", not any(isinstance(w, QMessageBox) and w.isVisible()
                                        for w in app.topLevelWidgets()))
    select(window, "hello.txt")
    close_modal(TextEditor)
    window.view()
    check("view button", not any(isinstance(w, TextEditor) and w.isVisible()
                                 for w in app.topLevelWidgets()))
    select(window, "hello.txt")
    close_modal(TextEditor, lambda dialog: (dialog.editor.setPlainText("edited"), dialog.accept()))
    window.edit()
    check("edit button saves", (local_root / "hello.txt").read_text() == "edited")

    fake_site = Site(name="Test server", protocol="SFTP", remote_path=str(remote_root))
    window.sessions.append({"site": fake_site, "fs": LocalFS(), "path": str(remote_root)})
    window.tabs.addTab("Test server")
    window.tabs.setVisible(True)
    window.tabs.setCurrentIndex(0)
    check("session tab and remote listing", wait_until(lambda: window.remote.path == str(remote_root)
                                                       and not window.jobs))
    with patch.object(QMessageBox, "information") as info:
        toolbar_click(window, "Открыть SSH-терминал")
    check("terminal rejects a non-SSH backend", info.called and not window.terminal_windows)
    window.set_active("local")
    select(window, "hello.txt")
    toolbar_click(window, "Копировать")
    check("copy button", wait_until(lambda: (remote_root / "hello.txt").exists()
          and not window.jobs))
    select(window, "move.txt")
    toolbar_click(window, "Переместить")
    check("move button", wait_until(lambda: (remote_root / "move.txt").exists()
          and not (local_root / "move.txt").exists() and not window.jobs))
    toolbar_click(window, "Сохранить сеанс")
    check("save session button", any(s["name"] == "Test server" for s in window.config["sites"]))
    close_modal(SyncDialog)
    toolbar_click(window, "Синхронизировать")
    check("sync button opens dialog", not any(isinstance(w, SyncDialog) and w.isVisible()
                                               for w in app.topLevelWidgets()))
    search_seen = []
    search_timer = QTimer()
    def close_search():
        for dialog in app.topLevelWidgets():
            if isinstance(dialog, QDialog) and dialog.isVisible() and dialog.windowTitle().startswith("Результаты поиска"):
                search_seen.append(True)
                dialog.accept()
                search_timer.stop()
                break
    search_timer.timeout.connect(close_search)
    search_timer.start(10)
    window.set_active("local")
    with patch.object(QInputDialog, "getText", return_value=("*.txt", True)):
        toolbar_click(window, "Найти файлы")
    check("find files button", wait_until(lambda: bool(search_seen) and not window.jobs))
    toolbar_click(window, "Отключиться")
    check("disconnect button", wait_until(lambda: not window.sessions and not window.jobs))

    window.resize(760, 470)
    check("resize", window.width() == 760 and window.height() == 470)
    window.move(35, 35)
    check("move window", window.pos().x() == 35 and window.pos().y() == 35)
    window.showMinimized()
    app.processEvents()
    check("minimize", window.isMinimized())
    window.showNormal()
    app.processEvents()
    check("restore", not window.isMinimized())

    loading_started = threading.Event()
    def loading(job):
        loading_started.set()
        while not job.cancelled.is_set():
            time.sleep(0.01)
        job.report(0, 1)
    window.submit("minimized loading", loading)
    check("load starts before minimize", wait_until(loading_started.is_set))
    window.showMinimized()
    app.processEvents()
    check("minimize during loading", window.isMinimized())
    window.showNormal()
    app.processEvents()
    check("restore during loading", not window.isMinimized())
    window.queue.setCurrentCell(window.queue.rowCount() - 1, 0)
    window.cancel_selected_job()
    check("cancel after restore", wait_until(lambda: not window.jobs))

    started = threading.Event()
    def slow(job):
        started.set()
        while not job.cancelled.is_set():
            time.sleep(0.01)
        job.report(0, 1)
    window.submit("slow cancel test", slow)
    check("background job starts", wait_until(started.is_set))
    window.queue.setCurrentCell(window.queue.rowCount() - 1, 0)
    window.cancel_selected_job()
    check("cancel background job", wait_until(lambda: not window.jobs))

    started.clear()
    closed_connection = threading.Event()
    class TrackingFS(LocalFS):
        def close(self):
            closed_connection.set()
    def late_connection(_job):
        started.set()
        time.sleep(0.2)
        return TrackingFS()
    window.submit("late connection during close", late_connection)
    check("background job starts before close", wait_until(started.is_set))
    quit_button = next(button for button in window.findChildren(QPushButton)
                       if button.text() == "F10 Quit")
    QTest.mouseClick(quit_button, Qt.MouseButton.LeftButton)
    check("close requests cancellation", window.closing)
    check("close finishes after worker", wait_until(lambda: window.shutdown_ready and not window.isVisible()))
    check("late connection is released", closed_connection.is_set())

print(f"PASS TOTAL {len(passed)} scenarios")
