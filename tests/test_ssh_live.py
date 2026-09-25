"""Optional real SSH integration test; requires a local disposable SSH server."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import SSHFS, Site


class SSHLiveTests(unittest.TestCase):
    def test_sftp_and_scp_round_trip(self):
        port = os.environ.get("BRIDGE_TEST_SSH_PORT")
        key = os.environ.get("BRIDGE_TEST_SSH_KEY")
        remote = os.environ.get("BRIDGE_TEST_SSH_REMOTE")
        if not all((port, key, remote)):
            self.skipTest("Local SSH test server not configured")
        user = os.environ.get("BRIDGE_TEST_SSH_USER") or os.environ["USER"]
        with tempfile.TemporaryDirectory() as root:
            known_hosts = Path(root) / "known_hosts"
            known_hosts.touch()
            with patch("paths.known_hosts_path", return_value=known_hosts):
                for protocol in ("SFTP", "SCP"):
                    site = Site(protocol=protocol, host="127.0.0.1", port=int(port),
                                username=user, key_file=key, remote_path=remote)
                    fs = SSHFS(site, lambda *_: True)
                    source = Path(root) / f"{protocol.lower()}.txt"
                    source.write_text(protocol, encoding="utf-8")
                    destination = remote + "/" + source.name
                    renamed = destination + ".renamed"
                    local_copy = Path(root) / (source.name + ".copy")
                    try:
                        fs.upload(str(source), destination)
                        self.assertIn(source.name, [e.name for e in fs.listdir(remote)])
                        fs.download(destination, str(local_copy))
                        self.assertEqual(local_copy.read_text(encoding="utf-8"), protocol)
                        fs.rename(destination, renamed)
                        self.assertTrue(fs.exists(renamed))
                        fs.delete(renamed, False)
                        self.assertFalse(fs.exists(renamed))
                    finally:
                        fs.close()


if __name__ == "__main__":
    unittest.main()
