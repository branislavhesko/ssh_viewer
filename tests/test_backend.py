import os
import urllib.request

import pytest

from app.backend import AuthRequired, ConnectParams, HostKeyChanged, HostKeyUnknown
from app.backend.py_backend import PyBackend
from app.backend.py_backend.range_server import parse_range


def test_listdir_sorts_folders_first_and_keeps_attrs(server, backend):
    (server.root / "b_dir").mkdir()
    (server.root / "a.txt").write_text("hello")
    os.symlink("a.txt", server.root / "link")
    entries = backend.listdir(str(server.root)).result(5)
    assert [e.name for e in entries] == ["b_dir", "a.txt", "link"]
    assert entries[0].is_dir and entries[1].size == 5 and entries[2].is_symlink
    assert entries[1].permissions.startswith("-rw")
    assert backend.stat(str(server.root / "link")).result(5).size == 5


def test_non_utf8_names_survive(server, backend):
    raw = os.fsencode(server.root) + b"/caf\xe9.txt"
    with open(raw, "wb") as out:
        out.write(b"x")
    (entry,) = backend.listdir(str(server.root)).result(5)
    assert backend.read(entry.path, 0, 10).result(5) == b"x"


def test_read_and_fetch(server, backend):
    data = os.urandom(3 * 256 * 1024 + 17)
    path = server.root / "blob"
    path.write_bytes(data)
    assert backend.read(str(path), 100, 50).result(5) == data[100:150]
    assert backend.read(str(path), len(data), 50).result(5) == b""
    assert backend.fetch(str(path)).result(5) == data


def get(url, headers=None, method="GET"):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.headers, response.read()


def test_range_server(server, backend):
    data = os.urandom(600_000)
    path = server.root / "video file.mp4"
    path.write_bytes(data)
    url = backend.open_url(str(path))
    assert url.startswith("http://127.0.0.1:")

    status, headers, body = get(url, method="HEAD")
    assert status == 200 and body == b""
    assert headers["Content-Length"] == "600000" and headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Type"] == "video/mp4"

    assert get(url)[2] == data
    status, headers, body = get(url, {"Range": "bytes=262140-262150"})
    assert status == 206 and body == data[262140:262151]
    assert headers["Content-Range"] == "bytes 262140-262150/600000"
    assert get(url, {"Range": "bytes=-10"})[2] == data[-10:]
    assert get(url, {"Range": "bytes=599990-"})[2] == data[599990:]
    with pytest.raises(urllib.error.HTTPError) as info:
        get(url, {"Range": "bytes=700000-"})
    assert info.value.code == 416


def test_range_server_rejects_bad_token(server, backend):
    (server.root / "f").write_text("secret")
    url = backend.open_url(str(server.root / "f"))
    token = url.split("/")[3]
    with pytest.raises(urllib.error.HTTPError) as info:
        get(url.replace(token, "x" * len(token)))
    assert info.value.code == 403


def test_range_server_missing_file(server, backend):
    with pytest.raises(urllib.error.HTTPError) as info:
        get(backend.open_url(str(server.root / "nope")))
    assert info.value.code == 404


def test_parse_range():
    assert parse_range(None, 10) is None
    assert parse_range("bytes=0-0", 10) == (0, 0)
    assert parse_range("bytes=5-100", 10) == (5, 9)
    assert parse_range("bytes=-3", 10) == (7, 9)
    assert parse_range("bytes=1-2,4-5", 10) is None
    with pytest.raises(ValueError):
        parse_range("bytes=10-", 10)


def test_block_cache_avoids_second_read(server, backend):
    path = server.root / "cached"
    path.write_bytes(os.urandom(1_000_000))
    backend.fetch(str(path)).result(5)
    issued = backend.reads_issued()
    backend.fetch(str(path)).result(5)
    assert backend.reads_issued() == issued


def test_same_size_rewrite_within_a_second_is_not_stale(server, backend):
    path = server.root / "frame"
    path.write_bytes(b"A" * 1000)
    mtime = path.stat().st_mtime
    assert backend.fetch(str(path)).result(5) == b"A" * 1000
    path.write_bytes(b"B" * 1000)
    os.utime(path, (mtime, mtime))
    backend.invalidate(str(path))
    assert backend.fetch(str(path)).result(5) == b"B" * 1000


def test_grep_returns_byte_offsets(server, backend):
    path = server.root / "log"
    path.write_bytes(b"one\ntwo ERROR\nthree\nerror four\n")
    assert backend.grep(str(path), "error").result(5) == [8, 20]
    assert backend.grep(str(path), "absent").result(5) == []


def test_search_streams_results(server, backend):
    import threading
    (server.root / "sub").mkdir()
    (server.root / "sub" / "Report.PDF").write_text("x")
    (server.root / "other.txt").write_text("x")
    found, done = [], threading.Event()
    backend.search(str(server.root), "report", found.extend, lambda e: done.set())
    assert done.wait(5)
    assert found == [str(server.root / "sub" / "Report.PDF")]


def test_shell_runs_commands_in_directory_on_a_pty(server, backend):
    import threading
    import time
    (server.root / "sub").mkdir()
    output, exited = bytearray(), []
    done = threading.Event()

    def finished(error):
        exited.append(error)
        done.set()
    shell = backend.open_shell(str(server.root / "sub"), 100, 30, output.extend, finished)
    # Markers built by the shell, so the echoed command line can't match.
    shell.write(b"echo A$((1+1))=$(stty size) B$((1+1))=$(pwd) L$((1+1))=${LANG:-unset}.\n")
    deadline = time.time() + 10
    while b"B2=" not in output and time.time() < deadline:
        time.sleep(0.02)
    shell.resize(120, 40)
    shell.write(b"echo C$((1+1))=$(stty size); exit\n")
    assert done.wait(10), bytes(output)
    text = bytes(output)
    assert exited == [None]
    assert b"A2=30 100" in text and b"C2=40 120" in text
    assert b"B2=" + str(server.root / "sub").encode() in text
    from app.backend.py_backend.backend import shell_environment
    expected = shell_environment().get("LANG")
    if expected:  # sent, and the test sshd accepts it
        assert f"L2={expected}.".encode() in text


def test_shell_close_ends_it_once(server, backend):
    import threading
    calls, done = [], threading.Event()
    shell = backend.open_shell(None, 80, 24, lambda _: None,
                               lambda error: (calls.append(error), done.set()))
    shell.write(b"sleep 30\n")
    shell.close()
    assert done.wait(10)
    shell.close()
    shell.write(b"ignored\n")
    assert calls == [None]


def test_unknown_host_key_then_trust(server, make_backend):
    server.known_hosts.write_text("")
    backend = make_backend()
    with pytest.raises(HostKeyUnknown) as info:
        backend.connect(server.params()).result(10)
    assert info.value.fingerprint.startswith("SHA256:")
    backend.trust_host_key(info.value).result(5)
    assert backend.connect(server.params()).result(10).startswith("/")


def test_missing_known_hosts_file_prompts_instead_of_trusting(server, make_backend):
    server.known_hosts.unlink()
    backend = make_backend()
    with pytest.raises(HostKeyUnknown):
        backend.connect(server.params()).result(10)


def test_changed_host_key_is_refused(server, make_backend):
    import asyncssh
    other = asyncssh.generate_private_key("ssh-ed25519")
    server.known_hosts.write_text(
        f"[127.0.0.1]:{server.port} " + other.export_public_key().decode())
    with pytest.raises(HostKeyChanged):
        make_backend().connect(server.params()).result(10)


def test_rejected_key_asks_for_credentials(server, make_backend, tmp_path):
    import asyncssh
    wrong = tmp_path / "wrong_key"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(str(wrong))
    os.chmod(wrong, 0o600)
    env = dict(os.environ)
    os.environ.pop("SSH_AUTH_SOCK", None)
    try:
        with pytest.raises(AuthRequired):
            make_backend().connect(server.params(identity_files=[str(wrong)])).result(10)
    finally:
        os.environ.update(env)


def test_ssh_config_alias(server, make_backend):
    server.ssh_config.write_text(
        f"Host myalias\n  HostName 127.0.0.1\n  Port {server.port}\n"
        f"  User {os.environ['USER']}\n  IdentityFile {server.client_key_file}\n")
    params = ConnectParams(host="myalias", known_hosts=str(server.known_hosts),
                           ssh_config=str(server.ssh_config))
    assert make_backend().connect(params).result(10).startswith("/")


def test_proxy_jump(server, make_backend, tmp_path):
    from conftest import Server
    (tmp_path / "target").mkdir()
    target = Server(tmp_path / "target")
    try:
        server.known_hosts.write_text(
            server.known_hosts.read_text() + target.known_hosts.read_text())
        (target.root / "behind").write_text("through the jump host")
        (tmp_path / "target" / "authorized_keys").write_bytes(
            (tmp_path / "authorized_keys").read_bytes())
        user = os.environ["USER"]
        server.ssh_config.write_text(
            f"Host jump\n  HostName 127.0.0.1\n  Port {server.port}\n  User {user}\n"
            f"  IdentityFile {server.client_key_file}\n"
            f"Host inside\n  HostName 127.0.0.1\n  Port {target.port}\n  User {user}\n"
            f"  IdentityFile {server.client_key_file}\n  ProxyJump jump\n")
        params = ConnectParams(host="inside", known_hosts=str(server.known_hosts),
                               ssh_config=str(server.ssh_config))
        backend = make_backend()
        backend.connect(params).result(10)
        assert backend.read(str(target.root / "behind"), 0, 7).result(5) == b"through"
    finally:
        target.stop()


def test_unknown_jump_host_key_is_reported_for_the_jump_host(server, make_backend, tmp_path):
    from conftest import Server
    (tmp_path / "target").mkdir()
    target = Server(tmp_path / "target")
    try:
        server.known_hosts.write_text(target.known_hosts.read_text())  # only the target is known
        (tmp_path / "target" / "authorized_keys").write_bytes(
            (tmp_path / "authorized_keys").read_bytes())
        user = os.environ["USER"]
        server.ssh_config.write_text(
            f"Host jump\n  HostName 127.0.0.1\n  Port {server.port}\n  User {user}\n"
            f"  IdentityFile {server.client_key_file}\n"
            f"Host inside\n  HostName 127.0.0.1\n  Port {target.port}\n  User {user}\n"
            f"  IdentityFile {server.client_key_file}\n  ProxyJump jump\n")
        params = ConnectParams(host="inside", known_hosts=str(server.known_hosts),
                               ssh_config=str(server.ssh_config))
        with pytest.raises(HostKeyUnknown) as info:
            make_backend().connect(params).result(10)
        assert info.value.port == server.port
    finally:
        target.stop()


def test_switching_hosts_does_not_serve_the_old_hosts_blocks(tmp_path, make_backend):
    from conftest import Server
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, second = Server(tmp_path / "a"), Server(tmp_path / "b")
    shared = tmp_path / "shared"
    backend = make_backend()
    try:
        shared.write_bytes(b"from first")
        stamp = shared.stat().st_mtime
        backend.connect(first.params(watcher="polling")).result(10)
        assert backend.fetch(str(shared)).result(5) == b"from first"
        shared.write_bytes(b"from other")
        os.utime(shared, (stamp, stamp))  # same path, size and mtime
        backend.connect(second.params(watcher="polling")).result(10)
        assert backend.fetch(str(shared)).result(5) == b"from other"
    finally:
        first.stop()
        second.stop()


def test_refused_channel_is_not_treated_as_connection_loss(server):
    import asyncio
    import time

    import asyncssh
    backend = PyBackend()
    backend.connect(server.params(watcher="polling")).result(10)

    async def refused():
        raise asyncssh.ChannelOpenError(1, "too many sessions")
    began = time.time()
    with pytest.raises(asyncssh.ChannelOpenError):
        asyncio.run_coroutine_threadsafe(
            backend._session.retrying(refused), backend._loop).result(5)
    assert time.time() - began < 1
    assert backend.listdir(str(server.root)).result(5) == []
    backend.close()


def test_close_fails_pending_and_later_calls_at_once(server, make_backend):
    import concurrent.futures
    import time
    path = server.root / "big"
    path.write_bytes(os.urandom(64 * 1024 * 1024))
    backend = make_backend()
    backend.connect(server.params(watcher="polling")).result(10)
    pending = backend.fetch(str(path))
    backend.close()
    began = time.time()
    try:
        pending.result(2)  # finishing first is fine too; hanging is not
    except (ConnectionError, concurrent.futures.CancelledError):
        pass
    with pytest.raises(ConnectionError):
        backend.stat(str(path)).result(2)
    assert time.time() - began < 2


def leftovers(folder):
    return [p.name for p in folder.iterdir() if ".rv-" in p.name]


def test_write_replaces_content_and_keeps_mode(server, backend):
    path = server.root / "run.sh"
    path.write_text("old")
    path.chmod(0o750)
    data = os.urandom(300_000)
    entry = backend.write(str(path), data).result(10)
    assert path.read_bytes() == data and entry.size == len(data)
    assert path.stat().st_mode & 0o7777 == 0o750
    assert leftovers(server.root) == []


def test_write_is_atomic_rename_not_in_place(server, backend):
    path = server.root / "f"
    path.write_text("old")
    inode = path.stat().st_ino
    backend.write(str(path), b"new").result(5)
    assert path.stat().st_ino != inode


def test_write_keeps_symlink_a_symlink(server, backend):
    target = server.root / "real.txt"
    target.write_text("old")
    link = server.root / "link.txt"
    link.symlink_to("real.txt")
    backend.write(str(link), b"new").result(5)
    assert link.is_symlink() and target.read_bytes() == b"new"


def test_write_creates_a_missing_file(server, backend):
    backend.write(str(server.root / "fresh"), b"hi").result(5)
    assert (server.root / "fresh").read_bytes() == b"hi"
    assert (server.root / "fresh").stat().st_mode & 0o777 == 0o644


def test_write_in_a_read_only_folder_falls_back_to_in_place(server, backend):
    folder = server.root / "locked"
    folder.mkdir()
    path = folder / "f"
    path.write_text("old")
    inode = path.stat().st_ino
    folder.chmod(0o555)
    try:
        backend.write(str(path), b"new content").result(5)
        assert path.read_bytes() == b"new content" and path.stat().st_ino == inode
    finally:
        folder.chmod(0o755)


def test_failed_write_keeps_the_old_file(server, backend):
    folder = server.root / "locked"
    folder.mkdir()
    path = folder / "f"
    path.write_text("old")
    path.chmod(0o444)
    folder.chmod(0o555)
    try:
        with pytest.raises(Exception):
            backend.write(str(path), b"new").result(5)
        assert path.read_text() == "old"
        assert leftovers(folder) == []
    finally:
        folder.chmod(0o755)


def test_read_after_write_sees_new_content_not_a_stale_handle(server, backend):
    path = server.root / "f"
    path.write_bytes(b"A" * 1000)
    assert backend.read(str(path), 0, 1000).result(5) == b"A" * 1000
    assert backend.fetch(str(path)).result(5) == b"A" * 1000
    backend.write(str(path), b"B" * 1000).result(5)
    assert backend.read(str(path), 0, 1000).result(5) == b"B" * 1000
    assert backend.fetch(str(path)).result(5) == b"B" * 1000
