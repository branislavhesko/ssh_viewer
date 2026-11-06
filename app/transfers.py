"""Uploads and downloads of files and folders, over Backend's per-file calls.

Free of Qt. Everything here runs on its own threads, so unlike backend callbacks
it may wait on Futures; ``Backend.close`` fails them, so nothing can hang the exit.

A job is one user action. It scans what it was given, asks once about anything
it would overwrite, makes the folders, then copies a few files at a time: small
files cost round trips, not bandwidth.
"""
from __future__ import annotations

import enum
import os
import posixpath
import secrets
import stat as statmod
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.backend import Backend, FileEntry, Transfer, TransferCancelled

PARALLEL_FILES = 4
# Remote names come from the server, which must not choose where we write.
UNSAFE_CHARACTERS = "/\\:" if os.name == "nt" else "/"
# Only readable by setting it, so once, before any threads start.
UMASK = os.umask(0o077)
os.umask(UMASK)


class Decision(enum.Enum):
    REPLACE = "replace"
    SKIP = "skip"
    CANCEL = "cancel"


class JobState(enum.Enum):
    WAITING = "waiting"
    SCANNING = "scanning"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Item:
    source: str
    target: str
    size: int
    mode: int
    done: int = 0


@dataclass
class Plan:
    folders: list[tuple[str, int]] = field(default_factory=list)  # parents first
    files: list[Item] = field(default_factory=list)


class Job:
    def __init__(self, upload: bool, sources: list[str], destination: str):
        self.upload = upload
        self.sources = sources
        self.destination = destination
        self.state = JobState.WAITING
        self.error = ""  # why the whole job failed
        self.failures: list[str] = []
        self.skipped: list[str] = []
        self.total_bytes = self.done_bytes = 0
        self.total_files = self.done_files = 0
        self._lock = threading.Lock()
        self._cancelled = False
        self._transfers: set[Transfer] = set()

    @property
    def finished(self) -> bool:
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            transfers = list(self._transfers)
        for transfer in transfers:
            transfer.cancel()

    def check(self) -> None:
        if self._cancelled:
            raise TransferCancelled()

    def fail(self, path: str, reason: object) -> None:
        with self._lock:
            self.failures.append(f"{path}: {reason}")

    def skip(self, path: str, reason: str) -> None:
        with self._lock:
            self.skipped.append(f"{path}: {reason}")

    def wait_for(self, transfer: Transfer) -> None:
        with self._lock:
            self._transfers.add(transfer)
            cancelled = self._cancelled
        if cancelled:
            transfer.cancel()
        try:
            transfer.future.result()
        finally:
            with self._lock:
                self._transfers.discard(transfer)

    def advance(self, item: Item, done: int) -> None:
        with self._lock:
            self.done_bytes += done - item.done
            item.done = done

    def file_done(self) -> None:
        with self._lock:
            self.done_files += 1

    def file_failed(self, item: Item, reason: object) -> None:
        """Keeps the progress bar about the files that can still finish."""
        with self._lock:
            self.done_bytes -= item.done
            self.total_bytes -= item.size
            self.total_files -= 1
            self.failures.append(f"{item.source}: {reason}")


def safe_name(name: str) -> bool:
    return name not in ("", ".", "..") and "\0" not in name \
        and not any(c in name for c in UNSAFE_CHARACTERS)


def local_kind(path: str) -> Optional[str]:
    if os.path.isdir(path):
        return "dir"
    return "file" if os.path.lexists(path) else None


class RemoteTree:
    """What already exists under a remote folder, one listing per folder.
    Folders missing on the server are never listed, so no call has to fail."""

    def __init__(self, backend: Backend, root: str):
        self._backend = backend
        self._listings = {root: self._list(root)}

    def _list(self, folder: str) -> dict[str, FileEntry]:
        return {entry.name: entry for entry in self._backend.listdir(folder).result()}

    def kind(self, path: str) -> Optional[str]:
        parent, name = posixpath.split(path)
        if parent not in self._listings:
            self._listings[parent] = self._list(parent) if self.kind(parent) == "dir" else {}
        entry = self._listings[parent].get(name)
        if entry is None:
            return None
        if entry.is_symlink:
            try:
                entry = self._backend.stat(entry.path).result()
            except Exception:
                return "file"  # a broken link
        return "dir" if entry.is_dir else "file"


def place(job: Job, plan: Plan, stack: list[tuple[str, str]], source: str, target: str,
          size: int, mode: int, linked: bool = False) -> None:
    """Add one scanned entry to the plan. Links to files are copied as files.
    Links to folders are left out, as they can loop, and are never recreated:
    a link planted by the other side could redirect what we write next."""
    if statmod.S_ISDIR(mode):
        if linked:
            job.skip(source, "link to a folder")
        else:
            plan.folders.append((target, statmod.S_IMODE(mode) | 0o700))
            stack.append((source, target))
    elif statmod.S_ISREG(mode):
        plan.files.append(Item(source, target, size, statmod.S_IMODE(mode)))
    else:
        job.skip(source, "not a regular file")


class TransferManager:
    def __init__(self, backend: Backend, ask: Callable[[Job, list[str]], Decision]):
        """``ask`` runs on a job thread and may block until the user answers."""
        self._backend = backend
        self._ask = ask
        self._jobs = ThreadPoolExecutor(1, thread_name_prefix="transfer-job")
        self._files = ThreadPoolExecutor(PARALLEL_FILES, thread_name_prefix="transfer-file")

    def upload(self, sources: list[str], directory: str) -> Job:
        return self._start(Job(True, sources, directory))

    def download(self, paths: list[str], directory: str) -> Job:
        return self._start(Job(False, paths, directory))

    def _start(self, job: Job) -> Job:
        self._jobs.submit(self._run, job)
        return job

    def _run(self, job: Job) -> None:
        state = JobState.DONE
        try:
            self._execute(job)
        except TransferCancelled:
            state = JobState.CANCELLED
        except Exception as exc:
            job.error = str(exc) or type(exc).__name__
            state = JobState.FAILED
        job.state = state

    def _execute(self, job: Job) -> None:
        job.check()
        job.state = JobState.SCANNING
        if job.upload:
            plan = self._scan_local(job)
            job.check()
            self._sort_out_clashes(job, plan, RemoteTree(self._backend, job.destination).kind, "/")
        else:
            plan = self._scan_remote(job)
            job.check()
            self._sort_out_clashes(job, plan, local_kind, os.sep)
        job.total_files = len(plan.files)
        job.total_bytes = sum(item.size for item in plan.files)
        job.state = JobState.RUNNING
        for folder, mode in plan.folders:
            job.check()
            try:
                if job.upload:
                    self._backend.mkdir(folder, mode).result()
                else:
                    os.makedirs(folder, exist_ok=True)
            except Exception as exc:
                job.fail(folder, exc)
        wait([self._files.submit(self._copy, job, item) for item in plan.files])
        job.check()

    # Scanning ----------------------------------------------------------------
    def _scan_local(self, job: Job) -> Plan:
        plan = Plan()
        stack: list[tuple[str, str]] = []
        for source in job.sources:
            source = os.path.normpath(source)
            try:
                info = os.stat(source)
            except OSError as exc:
                job.fail(source, exc.strerror or exc)
                continue
            target = posixpath.join(job.destination, os.path.basename(source))
            place(job, plan, stack, source, target, info.st_size, info.st_mode)
        while stack:
            job.check()
            folder, remote = stack.pop()
            try:
                with os.scandir(folder) as scan:
                    entries = list(scan)
            except OSError as exc:
                job.fail(folder, exc.strerror or exc)
                continue
            for entry in entries:
                try:
                    info = os.stat(entry.path)
                except OSError:
                    job.skip(entry.path, "broken link" if entry.is_symlink() else "unreadable")
                    continue
                place(job, plan, stack, entry.path, posixpath.join(remote, entry.name),
                      info.st_size, info.st_mode, entry.is_symlink())
        return plan

    def _scan_remote(self, job: Job) -> Plan:
        plan = Plan()
        stack: list[tuple[str, str]] = []
        for path in job.sources:
            name = posixpath.basename(path.rstrip("/"))
            if not safe_name(name):
                job.fail(path, "can't be used as a local name")
                continue
            try:
                info = self._backend.stat(path).result()
            except Exception as exc:
                job.fail(path, exc)
                continue
            place(job, plan, stack, path, os.path.join(job.destination, name), info.size, info.mode)
        while stack:
            job.check()
            folder, local = stack.pop()
            try:
                entries = self._backend.listdir(folder).result()
            except Exception as exc:
                job.fail(folder, exc)
                continue
            for entry in entries:
                if not safe_name(entry.name):
                    job.fail(entry.path, "can't be used as a local name")
                    continue
                info = entry
                if entry.is_symlink:
                    try:
                        info = self._backend.stat(entry.path).result()
                    except Exception:
                        job.skip(entry.path, "broken link")
                        continue
                place(job, plan, stack, entry.path, os.path.join(local, entry.name),
                      info.size, info.mode, entry.is_symlink)
        return plan

    def _sort_out_clashes(self, job: Job, plan: Plan, kind: Callable[[str], Optional[str]],
                          separator: str) -> None:
        """Folders merge into existing folders. A file in a folder's place, or
        the reverse, fails; files over files are for the user to decide."""
        blocked: list[str] = []

        def is_blocked(path: str) -> bool:
            return any(path.startswith(folder + separator) for folder in blocked)

        folders = []
        for folder, mode in plan.folders:
            job.check()
            if is_blocked(folder):
                continue
            if kind(folder) == "file":
                job.fail(folder, "a file already has this name")
                blocked.append(folder)
            else:
                folders.append((folder, mode))
        files, clashes = [], []
        for item in plan.files:
            job.check()
            if is_blocked(item.target):
                continue
            found = kind(item.target)
            if found == "dir":
                job.fail(item.target, "a folder already has this name")
            elif found == "file":
                clashes.append(item)
            else:
                files.append(item)
        plan.folders = folders
        plan.files = files
        if clashes:
            decision = self._ask(job, [item.target for item in clashes])
            if decision is Decision.CANCEL:
                job.cancel()
                job.check()
            if decision is Decision.REPLACE:
                files.extend(clashes)
            else:
                for item in clashes:
                    job.skip(item.target, "already exists")

    # Copying -----------------------------------------------------------------
    def _copy(self, job: Job, item: Item) -> None:
        try:
            job.check()
            if job.upload:
                job.wait_for(self._backend.upload_file(
                    item.source, item.target, item.mode & 0o777,
                    lambda done: job.advance(item, done)))
            else:
                self._receive(job, item)
            job.file_done()
        except TransferCancelled:
            pass
        except Exception as exc:
            job.file_failed(item, exc)

    def _receive(self, job: Job, item: Item) -> None:
        """Through a private temp file, so a failure leaves any old file alone
        and a secret is never readable by others while it arrives."""
        folder = os.path.dirname(item.target)
        temp = os.path.join(folder, f".rv-{secrets.token_hex(4)}.part")
        os.close(os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        try:
            job.wait_for(self._backend.download_file(item.source, temp,
                                                     lambda done: job.advance(item, done)))
            os.chmod(temp, item.mode & 0o777 & ~UMASK)
            os.replace(temp, item.target)
        except BaseException:
            with suppress(OSError):
                os.unlink(temp)
            raise
