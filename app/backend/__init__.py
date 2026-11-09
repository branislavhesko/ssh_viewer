from .interface import (Backend, ConnectParams, ConnectionState, Event, EventKind,
                        FileEntry, HostKeyChanged, HostKeyUnknown, AuthRequired, SearchHandle,
                        Transfer, TransferCancelled)

__all__ = ["Backend", "ConnectParams", "ConnectionState", "Event", "EventKind",
           "FileEntry", "HostKeyChanged", "HostKeyUnknown", "AuthRequired", "SearchHandle",
           "Transfer", "TransferCancelled", "create_backend"]


def create_backend(kind: str = "auto", cache_bytes: int = 512 * 1024 * 1024) -> Backend:
    """kind: "python", "rust", or "auto" (Rust core when it is installed)."""
    if kind in ("rust", "auto"):
        try:
            from .rust_backend import RustBackend
            return RustBackend(cache_bytes)
        except ImportError:
            if kind == "rust":
                raise
    from .py_backend import PyBackend
    return PyBackend(cache_bytes)
