"""Disposable local protocol servers; pyftpdlib and moto are test-only extras."""
from pathlib import Path
import logging
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend import FTPFS, S3FS, Site


class FTPIntegrationTests(unittest.TestCase):
    def test_ftp_round_trip(self):
        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.handlers import FTPHandler
            from pyftpdlib.servers import FTPServer
        except ImportError:
            self.skipTest("pyftpdlib not installed")
        with tempfile.TemporaryDirectory() as root:
            authorizer = DummyAuthorizer()
            authorizer.add_user("tester", "password", root, perm="elradfmwMT")
            handler = type("LocalFTPHandler", (FTPHandler,), {"authorizer": authorizer})
            server = FTPServer(("127.0.0.1", 0), handler)
            logging.getLogger("pyftpdlib").setLevel(logging.ERROR)
            stopped = threading.Event()
            def serve():
                while not stopped.is_set():
                    server.serve_forever(timeout=0.1, blocking=False)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            site = Site(protocol="FTP", host="127.0.0.1", port=server.socket.getsockname()[1],
                        username="tester", password="password")
            try:
                fs = FTPFS(site)
                source = Path(root) / "source.txt"
                source.write_text("FTP round trip", encoding="utf-8")
                downloaded = Path(root) / "downloaded.txt"
                try:
                    fs.mkdir("/folder")
                    fs.upload(str(source), "/folder/file.txt")
                    self.assertEqual([e.name for e in fs.listdir("/folder")], ["file.txt"])
                    fs.download("/folder/file.txt", str(downloaded))
                    self.assertEqual(downloaded.read_text(encoding="utf-8"), "FTP round trip")
                    fs.rename("/folder/file.txt", "/folder/renamed.txt")
                    self.assertTrue(fs.exists("/folder/renamed.txt"))
                    fs.delete("/folder", True)
                    self.assertFalse(fs.exists("/folder"))
                finally:
                    source.unlink(missing_ok=True)
                    downloaded.unlink(missing_ok=True)
                    fs.close()
            finally:
                stopped.set()
                thread.join(timeout=2)
                server.close_all()

    def test_explicit_ftps_with_verified_certificate(self):
        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.handlers import TLS_FTPHandler
            from pyftpdlib.servers import FTPServer
        except ImportError:
            self.skipTest("pyftpdlib FTPS support not installed")
        with tempfile.TemporaryDirectory() as root:
            cert = str(Path(root) / "cert.pem")
            key = str(Path(root) / "key.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-subj", "/CN=localhost", "-addext",
                            "subjectAltName=DNS:localhost,IP:127.0.0.1",
                            "-keyout", key, "-out", cert, "-days", "1"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            authorizer = DummyAuthorizer()
            authorizer.add_user("tester", "password", root, perm="elradfmwMT")
            handler = type("LocalTLSHandler", (TLS_FTPHandler,),
                           {"authorizer": authorizer, "certfile": cert, "keyfile": key,
                            "tls_control_required": True, "tls_data_required": True})
            server = FTPServer(("127.0.0.1", 0), handler)
            stopped = threading.Event()
            def serve():
                while not stopped.is_set():
                    server.serve_forever(timeout=0.1, blocking=False)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            context = ssl.create_default_context(cafile=cert)
            site = Site(protocol="FTPS", host="localhost", port=server.socket.getsockname()[1],
                        username="tester", password="password")
            try:
                with patch("backend.ssl.create_default_context", return_value=context):
                    fs = FTPFS(site)
                source = Path(root) / "source.txt"
                source.write_text("encrypted transfer", encoding="utf-8")
                target = Path(root) / "downloaded.txt"
                try:
                    fs.upload(str(source), "/encrypted.txt")
                    fs.download("/encrypted.txt", str(target))
                    self.assertEqual(target.read_text(encoding="utf-8"), "encrypted transfer")
                finally:
                    fs.close()
            finally:
                stopped.set()
                thread.join(timeout=2)
                server.close_all()

    def test_implicit_ftps_with_verified_certificate(self):
        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.handlers import TLS_FTPHandler
            from pyftpdlib.servers import FTPServer
        except ImportError:
            self.skipTest("pyftpdlib FTPS support not installed")
        with tempfile.TemporaryDirectory() as root:
            cert = str(Path(root) / "cert.pem")
            key = str(Path(root) / "key.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-subj", "/CN=localhost", "-addext",
                            "subjectAltName=DNS:localhost,IP:127.0.0.1",
                            "-keyout", key, "-out", cert, "-days", "1"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            authorizer = DummyAuthorizer()
            authorizer.add_user("tester", "password", root, perm="elradfmwMT")
            class ImplicitHandler(TLS_FTPHandler):
                def on_connect(self):
                    self.secure_connection(self.ssl_context)
                    super().on_connect()
            ImplicitHandler.authorizer = authorizer
            ImplicitHandler.certfile = cert
            ImplicitHandler.keyfile = key
            ImplicitHandler.tls_data_required = True
            server = FTPServer(("127.0.0.1", 0), ImplicitHandler)
            stopped = threading.Event()
            def serve():
                while not stopped.is_set():
                    server.serve_forever(timeout=0.1, blocking=False)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            context = ssl.create_default_context(cafile=cert)
            site = Site(protocol="FTPS Implicit", host="localhost",
                        port=server.socket.getsockname()[1],
                        username="tester", password="password")
            try:
                with patch("backend.ssl.create_default_context", return_value=context):
                    fs = FTPFS(site)
                source = Path(root) / "source.txt"
                source.write_text("implicit encrypted transfer", encoding="utf-8")
                target = Path(root) / "downloaded.txt"
                try:
                    fs.upload(str(source), "/encrypted.txt")
                    fs.download("/encrypted.txt", str(target))
                    self.assertEqual(target.read_text(encoding="utf-8"),
                                     "implicit encrypted transfer")
                finally:
                    fs.close()
            finally:
                stopped.set()
                thread.join(timeout=2)
                server.close_all()


class S3IntegrationTests(unittest.TestCase):
    def test_s3_round_trip(self):
        try:
            from moto import mock_aws
            import boto3
        except ImportError:
            self.skipTest("moto not installed")
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="bridge-test")
            fs = S3FS(Site(protocol="S3", remote_path="/bridge-test", region="us-east-1",
                           username="testing", password="testing"))
            with tempfile.TemporaryDirectory() as root:
                source = Path(root) / "source.txt"
                source.write_text("S3 round trip", encoding="utf-8")
                fs.mkdir("/bridge-test/folder")
                fs.upload(str(source), "/bridge-test/folder/file.txt")
                self.assertEqual([e.name for e in fs.listdir("/bridge-test/folder")], ["file.txt"])
                downloaded = Path(root) / "downloaded.txt"
                fs.download("/bridge-test/folder/file.txt", str(downloaded))
                self.assertEqual(downloaded.read_text(encoding="utf-8"), "S3 round trip")
                fs.rename("/bridge-test/folder/file.txt", "/bridge-test/folder/renamed.txt")
                self.assertTrue(fs.exists("/bridge-test/folder/renamed.txt"))
                fs.delete("/bridge-test/folder", True)
                self.assertFalse(fs.exists("/bridge-test/folder"))


if __name__ == "__main__":
    unittest.main()
