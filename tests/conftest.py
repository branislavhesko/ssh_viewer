"""Real sshd on a random port, run as the current user with throwaway keys."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

# Keep settings and cached files out of the real user profile.
_config = tempfile.TemporaryDirectory(prefix="rv-test-config-")
os.environ["XDG_CONFIG_HOME"] = _config.name
os.environ["XDG_CACHE_HOME"] = os.path.join(_config.name, "cache")

import asyncssh
import pytest

from app.backend import ConnectParams, create_backend

SSHD = shutil.which("sshd") or "/usr/sbin/sshd"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, base: Path, path_prefix: str = ""):
        self.base = base
        self.port = free_port()
        self.root = base / "data"
        self.root.mkdir()
        host_key = asyncssh.generate_private_key("ssh-ed25519")
        client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.host_key_file = base / "host_key"
        self.client_key_file = base / "client_key"
        host_key.write_private_key(str(self.host_key_file))
        client_key.write_private_key(str(self.client_key_file))
        os.chmod(self.host_key_file, 0o600)
        os.chmod(self.client_key_file, 0o600)
        (base / "authorized_keys").write_bytes(client_key.export_public_key())
        self.known_hosts = base / "known_hosts"
        self.known_hosts.write_text(
            f"[127.0.0.1]:{self.port} "
            + host_key.export_public_key().decode())
        self.ssh_config = base / "ssh_config"
        self.ssh_config.write_text("")
        path_line = f"SetEnv PATH={path_prefix}:/usr/bin:/bin\n" if path_prefix else ""
        (base / "sshd_config").write_text(
            f"Port {self.port}\nListenAddress 127.0.0.1\n"
            f"HostKey {self.host_key_file}\nPidFile {base}/sshd.pid\n"
            f"AuthorizedKeysFile {base}/authorized_keys\n"
            "StrictModes no\nUsePAM no\nPasswordAuthentication no\n"
            "Subsystem sftp internal-sftp\nMaxSessions 20\nAcceptEnv LANG LC_*\n" + path_line)
        self.process = subprocess.Popen(
            [SSHD, "-D", "-e", "-f", str(base / "sshd_config")],
            stdout=subprocess.DEVNULL, stderr=open(base / "sshd.log", "w"))
        deadline = time.time() + 5
        while time.time() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.02)
        raise RuntimeError((base / "sshd.log").read_text())

    def params(self, **overrides) -> ConnectParams:
        values = dict(host="127.0.0.1", port=self.port, username=os.environ["USER"],
                      identity_files=[str(self.client_key_file)],
                      known_hosts=str(self.known_hosts), ssh_config=str(self.ssh_config),
                      keepalive_interval=5)
        values.update(overrides)
        return ConnectParams(**values)

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=5)


@pytest.fixture
def server(tmp_path):
    instance = Server(tmp_path)
    yield instance
    instance.stop()


@pytest.fixture(params=["python", "rust"])
def make_backend(request):
    """Both implementations must pass every test that uses this."""
    if request.param == "rust":
        pytest.importorskip("rv_core")
    created = []

    def make():
        created.append(create_backend(request.param))
        return created[-1]
    make.kind = request.param
    yield make
    for instance in created:
        instance.close()


@pytest.fixture
def backend(server, make_backend):
    instance = make_backend()
    instance.connect(server.params(watcher="polling")).result(timeout=10)
    return instance


def inotify_dir():
    """Directory holding inotifywait for the test server, or None.

    RV_TEST_INOTIFY_DIR points at one when it isn't installed system wide.
    """
    configured = os.environ.get("RV_TEST_INOTIFY_DIR")
    if configured:
        return configured
    found = shutil.which("inotifywait")
    return os.path.dirname(found) if found else None


@pytest.fixture
def inotify_server(tmp_path):
    directory = inotify_dir()
    if directory is None:
        pytest.skip("inotifywait not available")
    instance = Server(tmp_path, path_prefix=directory)
    yield instance
    instance.stop()
