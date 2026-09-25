import threading
import time
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paramiko

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from backend import SSHFS, Site
from terminal import TerminalBridge, TerminalConsole, TerminalWindow


class FakeChannel:
    def __init__(self):
        self.closed = False
        self.sent = []
        self.sizes = []
        self.output = deque([b"\x1b[32mready\x1b[0m\r\n$ "])
        self.lock = threading.Lock()

    def sendall(self, data):
        with self.lock:
            self.sent.append(data)

    def recv_ready(self):
        with self.lock:
            return bool(self.output)

    def recv(self, _size):
        with self.lock:
            return self.output.popleft()

    def exit_status_ready(self):
        return False

    def resize_pty(self, width, height):
        with self.lock:
            self.sizes.append((width, height))

    def close(self):
        self.closed = True


class FakeSSH:
    def __init__(self, delay=0, error=None):
        self.channel = FakeChannel()
        self.delay = delay
        self.error = error
        self.calls = []

    def invoke_shell(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.channel


class LocalShellServer(paramiko.ServerInterface):
    def __init__(self):
        self.shell_requested = threading.Event()
        self.shell_channel = None
        self.commands = []

    def get_allowed_auths(self, _username):
        return "password"

    def check_auth_password(self, username, password):
        return (paramiko.AUTH_SUCCESSFUL if (username, password) == ("demo", "demo-pass")
                else paramiko.AUTH_FAILED)

    def check_channel_request(self, kind, _channel_id):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *_args):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_channel = channel
        self.shell_requested.set()
        return True


class TerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, condition, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return False

    def test_bridge_uses_existing_ssh_and_current_directory(self):
        ssh = FakeSSH()
        bridge = TerminalBridge(ssh, "/srv/demo project")
        bridge.start()
        self.assertTrue(self.wait_until(lambda: bool(ssh.channel.sent)))
        self.assertEqual(ssh.channel.sent[0], b"cd -- '/srv/demo project'\n")
        self.assertEqual(len(ssh.calls), 1)
        self.assertEqual(ssh.calls[0]["term"], "xterm")
        bridge.stop()
        self.assertTrue(self.wait_until(lambda: ssh.channel.closed))

    def test_window_keyboard_resize_minimize_and_close(self):
        ssh = FakeSSH()
        site = Site(name="Demo", protocol="SFTP", host="files.example.net",
                    port=22, username="demo")
        session = {"site": site, "fs": type("FS", (), {"ssh": ssh})(), "path": "/home/demo"}
        window = TerminalWindow(session, "/home/demo")
        window.show()
        self.assertTrue(self.wait_until(lambda: "ready" in window.console.toPlainText()))
        self.assertIn("/home/demo", window.status.text())
        QTest.keyClicks(window.console, "pwd")
        QTest.keyClick(window.console, Qt.Key.Key_Return)
        QTest.keyClick(window.console, Qt.Key.Key_Up)
        QTest.keyClick(window.console, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.wait_until(lambda: len(ssh.channel.sent) >= 7))
        self.assertIn(b"\r", ssh.channel.sent)
        self.assertIn(b"\x1b[A", ssh.channel.sent)
        self.assertIn(b"\x03", ssh.channel.sent)
        window.resize(900, 600)
        self.assertTrue(self.wait_until(lambda: bool(ssh.channel.sizes)))
        window.showMinimized()
        self.app.processEvents()
        self.assertTrue(window.isMinimized())
        window.showNormal()
        self.app.processEvents()
        self.assertFalse(window.isMinimized())
        window.close()
        self.assertTrue(self.wait_until(lambda: ssh.channel.closed))

    def test_terminal_output_parsing_and_bounded_history(self):
        console = TerminalConsole()
        console.append_output("hello\r")
        console.append_output("\n\x1b[31mworld\x1b[0m")
        self.assertEqual(console.toPlainText(), "hello\nworld")
        console.append_output("\rreplace")
        self.assertEqual(console.toPlainText(), "hello\nreplace")
        console.append_output("\x1b[2Jfresh")
        self.assertEqual(console.toPlainText(), "fresh")
        console.append_output("\n".join(str(i) for i in range(2100)))
        self.assertLessEqual(console.blockCount(), 2000)
        console.append_output("x" * 600000)
        self.assertLessEqual(console.document().characterCount(), 500000)

    def test_error_and_stop_while_connecting(self):
        failed = FakeSSH(error=OSError("shell unavailable"))
        site = Site(name="Demo", protocol="SFTP", host="files.example.net")
        session = {"site": site, "fs": type("FS", (), {"ssh": failed})(), "path": "/"}
        window = TerminalWindow(session, "/")
        window.show()
        self.assertTrue(self.wait_until(lambda: "shell unavailable" in window.status.text()))
        window.close()

        slow = FakeSSH(delay=0.15)
        bridge = TerminalBridge(slow, "/")
        bridge.start()
        bridge.stop()
        self.assertTrue(self.wait_until(lambda: slow.channel.closed))

    def test_real_ssh_connection_reuses_authenticated_transport(self):
        import socket

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)
        server = LocalShellServer()
        host_key = paramiko.RSAKey.generate(2048)
        transports = []
        stop = threading.Event()

        def serve():
            try:
                connection, _address = listener.accept()
                transport = paramiko.Transport(connection)
                transports.append(transport)
                transport.add_server_key(host_key)
                transport.start_server(server=server)
                while transport.is_active() and not stop.is_set():
                    channel = transport.accept(0.2)
                    if channel is None:
                        continue
                    deadline = time.monotonic() + 0.5
                    while server.shell_channel is not channel and not channel.closed and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if server.shell_channel is not channel:
                        channel.close()  # Refuse SFTP; the SCP connection stays alive.
                        continue
                    channel.sendall(b"demo$ ")
                    channel.settimeout(0.2)
                    buffer = b""
                    while not stop.is_set() and not channel.closed:
                        try:
                            data = channel.recv(4096)
                        except socket.timeout:
                            continue
                        if not data:
                            break
                        buffer += data
                        while b"\n" in buffer or b"\r" in buffer:
                            cut = min((index for index in (buffer.find(b"\n"), buffer.find(b"\r"))
                                       if index >= 0))
                            line, buffer = buffer[:cut], buffer[cut + 1:]
                            if not line:
                                continue
                            server.commands.append(line)
                            channel.sendall(b"ok: " + line + b"\r\ndemo$ ")
                    channel.close()
            finally:
                for transport in transports:
                    transport.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        bridge = None
        fs = None
        try:
            with TemporaryDirectory() as root, patch("paths.known_hosts_path", return_value=Path(root) / "known_hosts"):
                site = Site(protocol="SCP", host="127.0.0.1", port=listener.getsockname()[1],
                            username="demo", password="demo-pass")
                fs = SSHFS(site, lambda *_args: True)
                output = []
                bridge = TerminalBridge(fs.ssh, "/srv/demo")
                bridge.signals.output.connect(output.append)
                bridge.start()
                self.assertTrue(self.wait_until(lambda: b"cd -- /srv/demo" in server.commands))
                bridge.send(b"pwd\r")
                self.assertTrue(self.wait_until(lambda: b"pwd" in server.commands))
                self.assertTrue(self.wait_until(lambda: any("ok: pwd" in part for part in output)))
                self.assertTrue(fs.ssh.get_transport().is_active())
        finally:
            if bridge:
                bridge.stop()
            if fs:
                fs.close()
            stop.set()
            listener.close()
            thread.join(timeout=2)

    def test_disconnecting_session_closes_its_terminal(self):
        import ui

        class FakeFS:
            def __init__(self):
                self.ssh = FakeSSH()

            def listdir(self, _path):
                return []

            def close(self):
                self.ssh.channel.close()

        with TemporaryDirectory() as root, \
             patch("ui.load_config", return_value={"sites": [], "bookmarks": [],
                                                   "local_path": root, "theme": "light"}), \
             patch("ui.save_config"), patch("ui.SSHFS", FakeFS):
            window = ui.MainWindow()
            window.show()
            self.assertTrue(self.wait_until(lambda: not window.jobs))
            fs = FakeFS()
            site = Site(name="Demo", protocol="SFTP", host="files.example.net",
                        username="demo", remote_path="/home/demo")
            session = {"site": site, "fs": fs, "path": "/home/demo"}
            window.sessions.append(session)
            window.tabs.addTab(site.name)
            window.tabs.setVisible(True)
            self.assertTrue(self.wait_until(lambda: window.remote.path == "/home/demo" and not window.jobs))
            window.open_terminal()
            self.assertEqual(len(window.terminal_windows), 1)
            terminal = next(iter(window.terminal_windows))
            self.assertTrue(self.wait_until(lambda: bool(fs.ssh.channel.sent)))
            window.disconnect()
            self.assertTrue(self.wait_until(lambda: fs.ssh.channel.closed and not window.terminal_windows))
            self.assertFalse(terminal.isVisible())
            window.close()
            self.assertTrue(self.wait_until(lambda: not window.isVisible()))


if __name__ == "__main__":
    unittest.main()
