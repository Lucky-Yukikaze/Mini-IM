"""Bind each live upload to one initialized connection and one byte stream."""
from dataclasses import dataclass
from time import monotonic


UPLOAD_HEADER_PREFIX = b"MINIIMFILE2 "


@dataclass
class UploadLease:
    user_id: str
    intent: str
    file_id: str
    owner: object
    device_id: str
    generation: int
    offset: int
    touched: float
    stream_id: int | None = None


class UploadRegistry:
    def __init__(self, idle_timeout_ms: int = 900000):
        self.idle_timeout_ms = max(idle_timeout_ms, 0)
        self.generation = 0
        self.by_intent: dict[tuple[str, str], UploadLease] = {}
        self.by_file: dict[str, UploadLease] = {}

    def next_generation(self) -> int:
        self.generation += 1
        return self.generation

    def available(self, user_id: str, intent: str, owner: object, device_id: str, generation: int) -> bool:
        lease = self.by_intent.get((user_id, intent))
        return (lease is None or (lease.owner is owner and lease.stream_id is None)
            or (device_id and lease.device_id == device_id and generation > lease.generation)
            or (self.idle_timeout_ms > 0 and (monotonic() - lease.touched) * 1000 >= self.idle_timeout_ms))

    def grant(self, user_id: str, intent: str, file_id: str, owner: object, offset: int,
              device_id: str, generation: int) -> UploadLease:
        previous = self.by_intent.get((user_id, intent))
        if previous:
            self.release(previous)
        lease = UploadLease(user_id, intent, file_id, owner, device_id, generation, offset, monotonic())
        self.by_intent[(user_id, intent)] = lease
        self.by_file[file_id] = lease
        return lease

    def current(self, lease: UploadLease) -> bool:
        return self.by_file.get(lease.file_id) is lease

    def bind(self, owner: object, file_id: str, stream_id: int, offset: int) -> UploadLease | None:
        lease = self.by_file.get(file_id)
        if lease is None or lease.owner is not owner or lease.stream_id is not None or lease.offset != offset:
            return None
        lease.stream_id = stream_id
        lease.touched = monotonic()
        return lease

    def advance(self, lease: UploadLease, offset: int) -> None:
        lease.offset = offset
        lease.touched = monotonic()

    def release(self, lease: UploadLease) -> None:
        if self.current(lease):
            self.by_file.pop(lease.file_id)
            self.by_intent.pop((lease.user_id, lease.intent))

    def release_owner(self, owner: object, stream_id: int | None = None) -> None:
        for lease in list(self.by_file.values()):
            if lease.owner is owner and (stream_id is None or lease.stream_id == stream_id):
                self.release(lease)
