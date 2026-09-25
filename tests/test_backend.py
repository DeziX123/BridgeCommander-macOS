import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend import LocalFS, Site, WebDAVFS, copy_tree


class BackendTests(unittest.TestCase):
    def test_recursive_copy_and_overwrite_decision(self):
        fs = LocalFS()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            target = Path(root) / "target"
            (source / "nested").mkdir(parents=True)
            target.mkdir()
            (source / "nested" / "a.txt").write_text("new", encoding="utf-8")
            copied = copy_tree(fs, fs, str(source), str(target / "source"), True)
            self.assertTrue(copied)
            self.assertEqual((target / "source" / "nested" / "a.txt").read_text(), "new")
            (source / "nested" / "a.txt").write_text("changed", encoding="utf-8")
            copied = copy_tree(fs, fs, str(source), str(target / "source"), True,
                               on_overwrite=lambda _: False)
            self.assertFalse(copied)
            self.assertEqual((target / "source" / "nested" / "a.txt").read_text(), "new")

    def test_local_rename_and_delete(self):
        fs = LocalFS()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "old.txt"
            source.write_text("hello")
            fs.rename(str(source), str(Path(root) / "new.txt"))
            self.assertFalse(source.exists())
            self.assertTrue(fs.exists(str(Path(root) / "new.txt")))
            fs.delete(str(Path(root) / "new.txt"), False)
            self.assertEqual(fs.listdir(root), [])


class DAVHandler(BaseHTTPRequestHandler):
    objects = {"/dav/hello.txt": b"hello"}

    def log_message(self, *_):
        pass

    def _reply(self, status=200, body=b"", content_type="text/plain"):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def do_PROPFIND(self):
        if self.path != "/dav/":
            self._reply(404)
            return
        entries = [
            '<d:response><d:href>/dav/</d:href><d:propstat><d:prop>'
            '<d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>'
        ]
        if self.headers.get("Depth") == "1":
            for path, data in self.objects.items():
                entries.append(f'<d:response><d:href>{path}</d:href><d:propstat><d:prop>'
                               f'<d:resourcetype/><d:getcontentlength>{len(data)}</d:getcontentlength>'
                               '</d:prop></d:propstat></d:response>')
        xml = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' +
               "".join(entries) + '</d:multistatus>').encode()
        self._reply(207, xml, "application/xml")

    def do_GET(self):
        data = self.objects.get(self.path)
        self._reply(200, data) if data is not None else self._reply(404)

    def do_PUT(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        self.objects[self.path] = data
        self._reply(201)

    def do_MOVE(self):
        target = self.headers["Destination"].split(self.headers["Host"])[-1]
        self.objects[target] = self.objects.pop(self.path)
        self._reply(201)

    def do_DELETE(self):
        self.objects.pop(self.path, None)
        self._reply(204)


class WebDAVTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DAVHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()
        cls.server.server_close()

    def test_browse_upload_download_rename_delete_with_endpoint_path(self):
        DAVHandler.objects = {"/dav/hello.txt": b"hello"}
        port = self.server.server_address[1]
        site = Site(protocol="WebDAV HTTP", host=f"http://127.0.0.1:{port}/dav",
                    port=80, remote_path="/")
        fs = WebDAVFS(site)
        self.assertEqual([x.name for x in fs.listdir("/")], ["hello.txt"])
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "upload.txt"
            source.write_bytes(b"payload")
            fs.upload(str(source), "/upload.txt")
            target = Path(root) / "download.txt"
            fs.download("/upload.txt", str(target))
            self.assertEqual(target.read_bytes(), b"payload")
            fs.rename("/upload.txt", "/renamed.txt")
            self.assertEqual(DAVHandler.objects["/dav/renamed.txt"], b"payload")
            fs.delete("/renamed.txt", False)
            self.assertNotIn("/dav/renamed.txt", DAVHandler.objects)
        fs.close()


if __name__ == "__main__":
    unittest.main()
