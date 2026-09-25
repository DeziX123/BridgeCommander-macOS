"""A small interactive SSH terminal sharing an authenticated file session."""

from __future__ import annotations

import codecs
import queue
import shlex
import threading

from PySide6.QtCore import QObject, Qt, Signal, QSize
from PySide6.QtGui import QFont, QKeySequence, QTextCursor
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QVBoxLayout, QWidget


class TerminalSignals(QObject):
    output = Signal(str)
    ready = Signal()
    error = Signal(str)
    finished = Signal()


class TerminalBridge:
    """Owns a shell channel on an existing Paramiko SSHClient.

    All network I/O runs outside Qt's GUI thread. The channel is separate from
    the SFTP/SCP channel, so commands and file transfers can coexist.
    """

    def __init__(self, ssh, path: str):
        self.ssh = ssh
        self.path = path
        self.signals = TerminalSignals()
        self._stop = threading.Event()
        self._outgoing: queue.Queue[bytes] = queue.Queue(maxsize=256)
        self._channel = None
        self._size = (100, 30)
        self._thread = threading.Thread(target=self._run, name="bridge-ssh-terminal", daemon=True)

    def start(self):
        self._thread.start()

    def send(self, data: bytes):
        if self._stop.is_set():
            return
        for offset in range(0, len(data), 4096):
            try:
                self._outgoing.put_nowait(data[offset:offset + 4096])
            except queue.Full:
                self.signals.error.emit("Очередь ввода терминала заполнена")
                return

    def resize(self, columns: int, rows: int):
        self._size = (max(20, columns), max(8, rows))

    def stop(self):
        self._stop.set()
        channel = self._channel
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass

    def _run(self):
        channel = None
        try:
            columns, rows = self._size
            channel = self.ssh.invoke_shell(term="xterm", width=columns, height=rows)
            self._channel = channel
            if self._stop.is_set():
                return
            channel.sendall(("cd -- " + shlex.quote(self.path) + "\n").encode("utf-8"))
            self.signals.ready.emit()
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            previous_size = (columns, rows)
            while not self._stop.is_set():
                try:
                    while True:
                        channel.sendall(self._outgoing.get_nowait())
                except queue.Empty:
                    pass
                size = self._size
                if size != previous_size:
                    try:
                        channel.resize_pty(width=size[0], height=size[1])
                    except Exception:
                        pass  # A shell can still work when the server refuses a resize.
                    previous_size = size
                received = False
                while channel.recv_ready():
                    data = channel.recv(65536)
                    if not data:
                        break
                    received = True
                    output = decoder.decode(data)
                    if output:
                        self.signals.output.emit(output)
                if channel.closed or channel.exit_status_ready():
                    break
                if not received:
                    self._stop.wait(0.05)
            trailing = decoder.decode(b"", final=True)
            if trailing and not self._stop.is_set():
                self.signals.output.emit(trailing)
        except Exception as exc:
            if not self._stop.is_set():
                self.signals.error.emit(f"SSH-терминал: {exc}")
        finally:
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
            self.signals.finished.emit()


class TerminalConsole(QPlainTextEdit):
    input_sent = Signal(bytes)

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setMaximumBlockCount(2000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFont("Menlo", 11))
        self.setStyleSheet("QPlainTextEdit { background: #17191c; color: #e8e8e8; "
                           "border: 1px solid #505050; selection-background-color: #626b75; }")
        self._state = "text"
        self._csi = ""
        self._pending_cr = False

    def keyPressEvent(self, event):
        modifiers = event.modifiers()
        key = event.key()
        if event.matches(QKeySequence.StandardKey.Copy):
            if self.textCursor().hasSelection():
                self.copy()
                return
            if modifiers & Qt.KeyboardModifier.MetaModifier:
                return
        if event.matches(QKeySequence.StandardKey.Paste):
            pasted = QApplication.clipboard().text()
            if pasted:
                self.input_sent.emit(pasted.encode("utf-8"))
            return
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            return
        if modifiers & Qt.KeyboardModifier.ControlModifier and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            self.input_sent.emit(bytes([key - Qt.Key.Key_A + 1]))
            return
        special = {
            Qt.Key.Key_Return: b"\r", Qt.Key.Key_Enter: b"\r",
            Qt.Key.Key_Backspace: b"\x7f", Qt.Key.Key_Delete: b"\x1b[3~",
            Qt.Key.Key_Tab: b"\t", Qt.Key.Key_Left: b"\x1b[D",
            Qt.Key.Key_Right: b"\x1b[C", Qt.Key.Key_Up: b"\x1b[A",
            Qt.Key.Key_Down: b"\x1b[B", Qt.Key.Key_Home: b"\x1b[H",
            Qt.Key.Key_End: b"\x1b[F", Qt.Key.Key_PageUp: b"\x1b[5~",
            Qt.Key.Key_PageDown: b"\x1b[6~", Qt.Key.Key_Escape: b"\x1b",
        }
        data = special.get(key)
        if data is None and event.text():
            data = event.text().encode("utf-8")
        if data:
            self.input_sent.emit(data)

    def append_output(self, text: str):
        """Handle common shell control sequences while keeping scrollback bounded."""
        if len(text) > 131072:
            text = text[-131072:]
        pending = []
        operations = []

        def flush():
            if pending:
                operations.append(("text", "".join(pending)))
                pending.clear()

        for char in text:
            if self._pending_cr:
                self._pending_cr = False
                if char == "\n":
                    pending.append("\n")
                    continue
                flush()
                operations.append(("return", ""))
            if self._state == "escape":
                if char == "[":
                    self._state = "csi"
                    self._csi = ""
                elif char == "]":
                    self._state = "osc"
                else:
                    self._state = "text"
                continue
            if self._state == "csi":
                if "@" <= char <= "~":
                    if char == "J" and self._csi in ("2", "3"):
                        flush()
                        operations.append(("clear", ""))
                    self._state = "text"
                else:
                    self._csi += char
                    if len(self._csi) > 64:
                        self._state = "text"
                continue
            if self._state == "osc":
                if char == "\x07":
                    self._state = "text"
                elif char == "\x1b":
                    self._state = "osc_escape"
                continue
            if self._state == "osc_escape":
                self._state = "text" if char == "\\" else "osc"
                continue
            if char == "\x1b":
                flush()
                self._state = "escape"
            elif char == "\r":
                self._pending_cr = True
            elif char == "\b":
                flush()
                operations.append(("backspace", ""))
            elif char == "\x07":
                continue
            else:
                pending.append(char)
        flush()

        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        for action, value in operations:
            if action == "text":
                cursor.insertText(value)
            elif action == "return":
                cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
                cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                cursor.removeSelectedText()
            elif action == "backspace":
                cursor.deletePreviousChar()
            else:
                self.clear()
                cursor = self.textCursor()
        self.setTextCursor(cursor)
        if self.document().characterCount() > 500000:
            trim = QTextCursor(self.document())
            trim.setPosition(0)
            trim.setPosition(self.document().characterCount() - 350000,
                             QTextCursor.MoveMode.KeepAnchor)
            trim.removeSelectedText()
        self.ensureCursorVisible()


class TerminalWindow(QWidget):
    closed = Signal(object)

    def __init__(self, session: dict, path: str, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.session = session
        self._had_error = False
        site = session["site"]
        self.setWindowTitle(f"{site.name}: {path} — Bridge Commander")
        self.resize(760, 480)
        self.setMinimumSize(QSize(480, 300))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self.status = QLabel(f"Подключение к {site.host}…")
        self.console = TerminalConsole()
        layout.addWidget(self.status)
        layout.addWidget(self.console, 1)
        self.bridge = TerminalBridge(session["fs"].ssh, path)
        self.console.input_sent.connect(self.bridge.send)
        self.bridge.signals.output.connect(self.console.append_output)
        self.bridge.signals.ready.connect(lambda: self.status.setText(f"SSH · {site.host}:{site.port} · {path}"))
        self.bridge.signals.error.connect(self._show_error)
        self.bridge.signals.finished.connect(self._finished)
        self.bridge.start()
        self.console.setFocus()

    def _show_error(self, message):
        self._had_error = True
        self.status.setText(message)
        self.console.append_output("\r\n" + message + "\r\n")

    def _finished(self):
        if self.isVisible() and not self.bridge._stop.is_set() and not self._had_error:
            self.status.setText("SSH-соединение закрыто")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not hasattr(self, "bridge"):
            return
        metrics = self.console.fontMetrics()
        columns = self.console.viewport().width() // max(1, metrics.horizontalAdvance("M"))
        rows = self.console.viewport().height() // max(1, metrics.lineSpacing())
        self.bridge.resize(columns, rows)

    def closeEvent(self, event):
        self.bridge.stop()
        self.closed.emit(self)
        super().closeEvent(event)
