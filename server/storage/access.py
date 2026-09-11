"""Exclusive process ownership of a data root and its file store."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path


@contextmanager
def storage_access(data_root: Path, file_root: Path):
    # Never unlink lock files: a second inode would permit two owners on POSIX.
    roots = sorted({data_root.resolve(), file_root.resolve()}, key=str)
    with ExitStack() as stack:
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context((root / ".miniim-storage.lock").open("a+b"))
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError(f"storage is in use: {root}; stop the server before maintenance") from error
        yield
