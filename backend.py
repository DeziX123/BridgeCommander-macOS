"""File-system adapters for Bridge Commander.

All remote methods run in the UI worker thread. Paths use POSIX syntax remotely.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
import ftplib
import hashlib
import base64
import os
import posixpath
import shutil
import socket
import ssl
import stat
import threading
import xml.etree.ElementTree as ET

@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mtime: float = 0
    mode: int = 0
    owner: str = ""


@dataclass
class Site:
    name: str = "Новое подключение"
    group: str = "My Workspace"
    protocol: str = "SFTP"
    host: str = ""
    port: int = 22
    username: str = ""
    password: str = ""
    key_file: str = ""
    remote_path: str = "/"
    local_path: str = ""
    region: str = ""
    session_token: str = ""

    def public_dict(self):
        data = asdict(self)
        data.pop("password")
        data.pop("session_token")
        return data


def clean_remote(path: str) -> str:
    path = posixpath.normpath("/" + path.lstrip("/"))
    return path if path.startswith("/") else "/" + path


class Cancelled(Exception):
    pass


class FS:
    def listdir(self, path: str) -> list[Entry]:
        raise NotImplementedError

    def download(self, source: str, target: str, progress=None):
        raise NotImplementedError

    def upload(self, source: str, target: str, progress=None):
        raise NotImplementedError

    def mkdir(self, path: str):
        raise NotImplementedError

    def delete(self, path: str, is_dir: bool):
        raise NotImplementedError

    def rename(self, source: str, target: str):
        raise NotImplementedError

    def chmod(self, path: str, mode: int):
        raise NotImplementedError("Этот протокол не поддерживает изменение прав")

    def close(self):
        pass

    def exists(self, path: str) -> bool:
        try:
            parent, name = posixpath.split(clean_remote(path))
            return any(e.name == name for e in self.listdir(parent or "/"))
        except FileNotFoundError:
            return False


class LocalFS(FS):
    def listdir(self, path, cancel=None):
        out = []
        for p in Path(path).iterdir():
            if cancel is not None and cancel.is_set():
                raise Cancelled("Операция отменена")
            try:
                s = p.stat()
                is_dir = p.is_dir() and not p.is_symlink()
                out.append(Entry(p.name, str(p), is_dir, 0 if is_dir else s.st_size,
                                 s.st_mtime, s.st_mode))
            except (PermissionError, FileNotFoundError):
                continue
        return sorted(out, key=lambda e: (not e.is_dir, e.name.casefold()))

    def download(self, source, target, progress=None):
        self._copy(source, target, progress)

    def upload(self, source, target, progress=None):
        self._copy(source, target, progress)

    def _copy(self, source, target, progress):
        total = os.path.getsize(source)
        copied = 0
        with open(source, "rb") as src, open(target, "wb") as dst:
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
                copied += len(chunk)
                if progress:
                    progress(copied, total)
        shutil.copystat(source, target)

    def mkdir(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    def delete(self, path, is_dir):
        if is_dir and not Path(path).is_symlink():
            shutil.rmtree(path)
        else:
            Path(path).unlink()

    def rename(self, source, target):
        Path(source).rename(target)

    def chmod(self, path, mode):
        os.chmod(path, mode)

    def exists(self, path):
        return Path(path).exists()


class SSHFS(FS):
    def __init__(self, site: Site, trust_callback):
        import paramiko

        self.site = site
        self.ssh = paramiko.SSHClient()
        self.ssh.load_system_host_keys()
        from paths import known_hosts_path
        self.known_hosts = known_hosts_path()
        if self.known_hosts.exists():
            self.ssh.load_host_keys(str(self.known_hosts))

        outer = self

        class AskPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
                if not trust_callback(hostname, key.get_name(), fingerprint):
                    raise paramiko.SSHException("Ключ сервера не принят")
                client.get_host_keys().add(hostname, key.get_name(), key)
                client.save_host_keys(str(outer.known_hosts))

        self.ssh.set_missing_host_key_policy(AskPolicy())
        args = dict(hostname=site.host, port=site.port, username=site.username or None,
                    timeout=15, auth_timeout=20, banner_timeout=15,
                    look_for_keys=True, allow_agent=True)
        if site.password:
            args["password"] = site.password
        if site.key_file:
            args["key_filename"] = site.key_file
        self.ssh.connect(**args)
        self.sftp = None
        if site.protocol == "SFTP":
            self.sftp = self.ssh.open_sftp()
        else:
            try:
                self.sftp = self.ssh.open_sftp()
            except Exception:
                pass  # Some SCP-only servers have no SFTP subsystem.
        if self.sftp:
            self.sftp.get_channel().settimeout(30)

    def _shell(self, command):
        _, stdout, stderr = self.ssh.exec_command(command, timeout=30)
        output = stdout.read()
        error = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if code:
            raise OSError(error.strip() or f"Команда завершилась с кодом {code}")
        return output

    def listdir(self, path):
        path = clean_remote(path)
        if self.sftp:
            out = []
            for a in self.sftp.listdir_attr(path):
                is_dir = stat.S_ISDIR(a.st_mode)
                out.append(Entry(a.filename, posixpath.join(path, a.filename), is_dir,
                                 0 if is_dir else a.st_size, a.st_mtime, a.st_mode,
                                 str(a.st_uid)))
            return sorted(out, key=lambda e: (not e.is_dir, e.name.casefold()))
        # POSIX shell fallback for SCP-only servers; NUL-separated names allow spaces/newlines.
        import shlex
        q = shlex.quote(path)
        script = (f'p={q}; [ -d "$p" ] || exit 1; for f in "$p"/* "$p"/.[!.]* "$p"/..?*; do '
                  '[ -e "$f" ] || continue; '
                  'printf "%s\\0" "${f##*/}"; '
                  'if [ -d "$f" ]; then printf "d\\0"; else printf "f\\0"; fi; '
                  'v=$(stat -c "%s %Y %a" -- "$f" 2>/dev/null || '
                  'stat -f "%z %m %Lp" "$f") || exit 1; '
                  'set -- $v; printf "%s\\0%s\\0%s\\0" "$1" "$2" "$3"; done')
        parts = self._shell(script).decode("utf-8", "replace").split("\0")
        out = []
        for i in range(0, len(parts) - 4, 5):
            name, kind, size, mtime, mode = parts[i:i+5]
            if name:
                out.append(Entry(name, posixpath.join(path, name), kind == "d",
                                 int(size or 0), float(mtime or 0), int(mode or "0", 8)))
        return sorted(out, key=lambda e: (not e.is_dir, e.name.casefold()))

    def download(self, source, target, progress=None):
        if self.site.protocol == "SCP":
            from scp import SCPClient
            with SCPClient(self.ssh.get_transport(), progress=lambda _n, size, sent: progress(sent, size) if progress else None) as scp:
                scp.get(source, target)
        else:
            self.sftp.get(source, target, callback=progress)

    def upload(self, source, target, progress=None):
        if self.site.protocol == "SCP":
            from scp import SCPClient
            with SCPClient(self.ssh.get_transport(), progress=lambda _n, size, sent: progress(sent, size) if progress else None) as scp:
                scp.put(source, target)
        else:
            self.sftp.put(source, target, callback=progress)

    def mkdir(self, path):
        if self.sftp:
            self.sftp.mkdir(path)
        else:
            import shlex
            self._shell("mkdir -- " + shlex.quote(path))

    def delete(self, path, is_dir):
        if self.sftp:
            if is_dir:
                for e in self.listdir(path):
                    self.delete(e.path, e.is_dir)
                self.sftp.rmdir(path)
            else:
                self.sftp.remove(path)
        else:
            import shlex
            self._shell("rm -r -- " + shlex.quote(path))

    def rename(self, source, target):
        if self.sftp:
            self.sftp.rename(source, target)
        else:
            import shlex
            self._shell("mv -- " + shlex.quote(source) + " " + shlex.quote(target))

    def chmod(self, path, mode):
        if self.sftp:
            self.sftp.chmod(path, mode)
        else:
            import shlex
            self._shell("chmod " + format(mode, "o") + " -- " + shlex.quote(path))

    def close(self):
        if self.sftp:
            self.sftp.close()
        self.ssh.close()


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    def connect(self, host="", port=0, timeout=-999, source_address=None):
        self.host = host or self.host
        self.port = port or 990
        if timeout != -999:
            self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), self.timeout,
                                           source_address=source_address)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def login(self, user="anonymous", passwd="", acct=""):
        return ftplib.FTP.login(self, user, passwd, acct)


class FTPFS(FS):
    def __init__(self, site):
        self.site = site
        if site.protocol == "FTPS Implicit":
            self.ftp = ImplicitFTP_TLS(context=ssl.create_default_context())
        elif site.protocol == "FTPS":
            self.ftp = ftplib.FTP_TLS(context=ssl.create_default_context())
        else:
            self.ftp = ftplib.FTP()
        self.ftp.connect(site.host, site.port, timeout=20)
        self.ftp.login(site.username or "anonymous", site.password or "")
        if site.protocol in ("FTPS", "FTPS Implicit"):
            self.ftp.prot_p()
        self.ftp.set_pasv(True)

    def listdir(self, path):
        path = clean_remote(path)
        out = []
        try:
            records = list(self.ftp.mlsd(path))
            for name, facts in records:
                name = posixpath.basename(name.rstrip("/"))
                if name in (".", ".."):
                    continue
                kind = facts.get("type") == "dir"
                stamp = facts.get("modify", "")
                try:
                    mtime = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    mtime = 0
                out.append(Entry(name, posixpath.join(path, name), kind,
                                 int(facts.get("size", 0)), mtime))
        except ftplib.error_perm:
            # Fallback for servers without MLSD.
            previous = self.ftp.pwd()
            self.ftp.cwd(path)
            try:
                for name in self.ftp.nlst():
                    name = posixpath.basename(name.rstrip("/"))
                    if name in (".", ".."):
                        continue
                    try:
                        self.ftp.cwd(name)
                        is_dir = True
                        self.ftp.cwd(path)
                    except ftplib.error_perm:
                        is_dir = False
                    try:
                        size = 0 if is_dir else int(self.ftp.size(name) or 0)
                    except ftplib.all_errors:
                        size = 0
                    out.append(Entry(name, posixpath.join(path, name), is_dir, size))
            finally:
                self.ftp.cwd(previous)
        return sorted(out, key=lambda e: (not e.is_dir, e.name.casefold()))

    def download(self, source, target, progress=None):
        try:
            total = int(self.ftp.size(source) or 0)
        except ftplib.all_errors:
            total = 0
        count = 0
        with open(target, "wb") as f:
            def write(data):
                nonlocal count
                f.write(data)
                count += len(data)
                if progress:
                    progress(count, total)
            self.ftp.retrbinary("RETR " + source, write)

    def upload(self, source, target, progress=None):
        total = os.path.getsize(source)
        count = 0
        with open(source, "rb") as f:
            def sent(data):
                nonlocal count
                count += len(data)
                if progress:
                    progress(count, total)
            self.ftp.storbinary("STOR " + target, f, callback=sent)

    def mkdir(self, path):
        self.ftp.mkd(path)

    def delete(self, path, is_dir):
        if is_dir:
            for e in self.listdir(path):
                self.delete(e.path, e.is_dir)
            self.ftp.rmd(path)
        else:
            self.ftp.delete(path)

    def rename(self, source, target):
        self.ftp.rename(source, target)

    def close(self):
        try:
            if self.ftp.sock:
                self.ftp.sock.settimeout(1)
            self.ftp.quit()
        except ftplib.all_errors:
            self.ftp.close()


class WebDAVFS(FS):
    NS = {"d": "DAV:"}

    def __init__(self, site):
        import requests

        self.site = site
        scheme = "https" if site.protocol == "WebDAV HTTPS" else "http"
        endpoint = site.host if "://" in site.host else f"{scheme}://{site.host}"
        parsed = urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Некорректный адрес WebDAV")
        authority = parsed.netloc
        if parsed.port is None and site.port not in (80, 443):
            authority += f":{site.port}"
        self.root = f"{parsed.scheme}://{authority}"
        self.base_path = parsed.path.rstrip("/")
        self.session = requests.Session()
        if site.username:
            self.session.auth = (site.username, site.password)
        self.session.headers["User-Agent"] = "BridgeCommander/1.0"
        response = self._request("PROPFIND", site.remote_path or "/", headers={"Depth": "0"})
        if response.status_code not in (200, 207):
            raise OSError(f"WebDAV: HTTP {response.status_code} {response.reason}")

    def _url(self, path):
        return self.root + quote(self.base_path + clean_remote(path), safe="/")

    def _request(self, method, path, **kwargs):
        response = self.session.request(method, self._url(path), timeout=(10, 30), **kwargs)
        if response.status_code >= 400:
            raise OSError(f"WebDAV: HTTP {response.status_code} {response.reason}")
        return response

    def listdir(self, path):
        path = clean_remote(path)
        response = self._request("PROPFIND", path, headers={"Depth": "1"})
        if response.status_code != 207:
            raise OSError("Сервер не поддерживает просмотр WebDAV (PROPFIND)")
        root = ET.fromstring(response.content)
        out = []
        for node in root.findall("d:response", self.NS):
            href = node.findtext("d:href", default="", namespaces=self.NS)
            item_path = unquote(urlparse(href).path).rstrip("/") or "/"
            if self.base_path and item_path.startswith(self.base_path + "/"):
                item_path = item_path[len(self.base_path):]
            elif item_path == self.base_path:
                item_path = "/"
            elif not item_path.startswith("/"):
                item_path = posixpath.join(path, item_path)
            if item_path == path.rstrip("/") or not item_path:
                continue
            props = node.find(".//d:prop", self.NS)
            if props is None:
                continue
            kind = props.find("d:resourcetype/d:collection", self.NS) is not None
            size = int(props.findtext("d:getcontentlength", default="0", namespaces=self.NS) or 0)
            modified = props.findtext("d:getlastmodified", default="", namespaces=self.NS)
            try:
                mtime = parsedate_to_datetime(modified).timestamp()
            except (ValueError, TypeError):
                mtime = 0
            name = posixpath.basename(item_path)
            if name:
                out.append(Entry(name, item_path, kind, size, mtime))
        return sorted(out, key=lambda e: (not e.is_dir, e.name.casefold()))

    def download(self, source, target, progress=None):
        response = self._request("GET", source, stream=True)
        total = int(response.headers.get("Content-Length", "0"))
        count = 0
        try:
            with open(target, "wb") as f:
                for chunk in response.iter_content(1024 * 1024):
                    f.write(chunk)
                    count += len(chunk)
                    if progress:
                        progress(count, total)
        finally:
            response.close()

    def upload(self, source, target, progress=None):
        total = os.path.getsize(source)
        count = 0
        def chunks():
            nonlocal count
            with open(source, "rb") as f:
                while data := f.read(1024 * 1024):
                    count += len(data)
                    if progress:
                        progress(count, total)
                    yield data
        self._request("PUT", target, data=chunks(), headers={"Content-Length": str(total)})

    def mkdir(self, path):
        self._request("MKCOL", path)

    def delete(self, path, is_dir):
        self._request("DELETE", path)

    def rename(self, source, target):
        self._request("MOVE", source, headers={"Destination": self._url(target), "Overwrite": "F"})

    def close(self):
        self.session.close()


class S3FS(FS):
    def __init__(self, site):
        import boto3
        from botocore.config import Config

        args = {"region_name": site.region or None,
                "config": Config(connect_timeout=10, read_timeout=30,
                                 retries={"max_attempts": 2})}
        if site.host:
            endpoint = site.host if "://" in site.host else "https://" + site.host
            parsed = urlparse(endpoint)
            if parsed.port is None and site.port not in (80, 443):
                endpoint = parsed._replace(netloc=parsed.netloc + f":{site.port}").geturl()
            args["endpoint_url"] = endpoint
        if site.username:
            args["aws_access_key_id"] = site.username
            args["aws_secret_access_key"] = site.password
        if site.session_token:
            args["aws_session_token"] = site.session_token
        self.client = boto3.client("s3", **args)
        bucket, _ = self._split(site.remote_path or "/")
        if bucket:
            self.client.head_bucket(Bucket=bucket)
        else:
            self.client.list_buckets()

    def _split(self, path):
        bits = clean_remote(path).strip("/").split("/", 1)
        return (bits[0], bits[1] if len(bits) > 1 else "") if bits[0] else ("", "")

    def listdir(self, path):
        bucket, prefix = self._split(path)
        if not bucket:
            return [Entry(b["Name"], "/" + b["Name"], True) for b in self.client.list_buckets()["Buckets"]]
        prefix = prefix.rstrip("/") + "/" if prefix else ""
        result = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for p in page.get("CommonPrefixes", []):
                key = p["Prefix"].rstrip("/")
                result.append(Entry(posixpath.basename(key), "/" + bucket + "/" + key, True))
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key == prefix or key.endswith("/"):
                    continue
                result.append(Entry(posixpath.basename(key), "/" + bucket + "/" + key,
                                    False, obj["Size"], obj["LastModified"].timestamp()))
        return sorted(result, key=lambda e: (not e.is_dir, e.name.casefold()))

    def download(self, source, target, progress=None):
        bucket, key = self._split(source)
        meta = self.client.head_object(Bucket=bucket, Key=key)
        total = meta["ContentLength"]
        count = 0
        def callback(n):
            nonlocal count
            count += n
            if progress:
                progress(count, total)
        self.client.download_file(bucket, key, target, Callback=callback)

    def upload(self, source, target, progress=None):
        bucket, key = self._split(target)
        total = os.path.getsize(source)
        count = 0
        def callback(n):
            nonlocal count
            count += n
            if progress:
                progress(count, total)
        self.client.upload_file(source, bucket, key, Callback=callback)

    def mkdir(self, path):
        bucket, key = self._split(path)
        if not bucket or not key:
            raise OSError("Создание бакетов здесь не поддерживается")
        self.client.put_object(Bucket=bucket, Key=key.rstrip("/") + "/", Body=b"")

    def delete(self, path, is_dir):
        bucket, key = self._split(path)
        if not key:
            raise OSError("Удаление бакета здесь не поддерживается")
        if is_dir:
            prefix = key.rstrip("/") + "/"
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                objects = [{"Key": x["Key"]} for x in page.get("Contents", [])]
                if objects:
                    self.client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        else:
            self.client.delete_object(Bucket=bucket, Key=key)

    def rename(self, source, target):
        sb, sk = self._split(source)
        tb, tk = self._split(target)
        entries = self.listdir(source) if any(e.path == source and e.is_dir for e in self.listdir(posixpath.dirname(source))) else None
        if entries is not None:
            prefix = sk.rstrip("/") + "/"
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=sb, Prefix=prefix):
                for obj in page.get("Contents", []):
                    old = obj["Key"]
                    new = tk.rstrip("/") + "/" + old[len(prefix):]
                    self.client.copy_object(Bucket=tb, Key=new, CopySource={"Bucket": sb, "Key": old})
            self.delete(source, True)
        else:
            self.client.copy_object(Bucket=tb, Key=tk, CopySource={"Bucket": sb, "Key": sk})
            self.client.delete_object(Bucket=sb, Key=sk)


def connect(site: Site, trust_callback=lambda *_: False) -> FS:
    if site.protocol in ("SFTP", "SCP"):
        return SSHFS(site, trust_callback)
    if site.protocol in ("FTP", "FTPS", "FTPS Implicit"):
        return FTPFS(site)
    if site.protocol in ("WebDAV HTTP", "WebDAV HTTPS"):
        return WebDAVFS(site)
    if site.protocol == "S3":
        return S3FS(site)
    raise ValueError(f"Неизвестный протокол: {site.protocol}")


def copy_tree(source_fs: FS, target_fs: FS, source: str, target: str,
              is_dir: bool, progress=None, on_overwrite=None):
    """Transfer a file/directory between a local and remote FS."""
    if is_dir:
        if not target_fs.exists(target):
            target_fs.mkdir(target)
        all_copied = True
        for item in source_fs.listdir(source):
            child_target = (os.path.join(target, item.name) if isinstance(target_fs, LocalFS)
                            else posixpath.join(target, item.name))
            copied = copy_tree(source_fs, target_fs, item.path, child_target, item.is_dir,
                               progress, on_overwrite)
            all_copied = all_copied and copied
        return all_copied
    if target_fs.exists(target) and on_overwrite is not None and not on_overwrite(target):
        return False
    if isinstance(source_fs, LocalFS):
        target_fs.upload(source, target, progress)
    elif isinstance(target_fs, LocalFS):
        source_fs.download(source, target, progress)
    else:
        raise NotImplementedError("Прямое копирование между двумя серверами пока не поддерживается")
    return True
