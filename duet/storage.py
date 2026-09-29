import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

from .core import SyncError

DEFAULT_HOME = Path.home() / "Library/Application Support/Duet"


class Store:
    def __init__(self, root=DEFAULT_HOME):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def read(self, name, default=None):
        path = self.root / (name + ".json")
        if not path.exists():
            return default
        with path.open() as f:
            return json.load(f)

    def write(self, name, data):
        fd, temporary = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.root / (name + ".json"))
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def remove(self, name):
        (self.root / (name + ".json")).unlink(missing_ok=True)

    @contextlib.contextmanager
    def lock(self, wait=0):
        with (self.root / "sync.lock").open("w") as handle:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SyncError("Another sync is already running. Try again in a minute.")
                    time.sleep(1)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
