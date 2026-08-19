from __future__ import annotations

import os
import re
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

MAX_LINE_BYTES = 256 * 1024
_SPECTATOR_START_MARKERS = (b"Start Spectator Game", b"Begin Spectating 1st player", b"Begin Spectating 2nd player")
_SPECTATOR_END_MARKERS = (b"End Spectator Mode", b"End Spectator Game")
_GAMESTATE_CREATE_RE = re.compile(
    rb"(?m)^[^\r\n]*GameState\.DebugPrintPower\(\)\s+-\s+CREATE_GAME[^\r\n]*$"
)
_BARE_CREATE_RE = re.compile(rb"(?m)^\s*CREATE_GAME\s*\r?$")


def _expanded_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


class PowerLogLocator:
    def __init__(self, configured_path: str = "") -> None:
        self.configured_path = configured_path.strip()

    def candidates(self) -> list[Path]:
        if self.configured_path:
            configured = _expanded_path(self.configured_path)
            if configured.is_dir():
                return [configured / "Power.log"]
            return [configured]

        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            return []
        hearthstone_root = Path(local_app_data) / "Blizzard" / "Hearthstone"
        log_roots = [hearthstone_root / "Logs"]
        if hearthstone_root.is_dir():
            try:
                log_roots.extend(path / "Logs" for path in hearthstone_root.iterdir() if path.is_dir())
            except OSError:
                pass
        candidates: list[Path] = []
        for logs_root in log_roots:
            candidates.append(logs_root / "Power.log")
            if logs_root.is_dir():
                try:
                    candidates.extend(logs_root.glob("*/Power.log"))
                    candidates.extend(logs_root.glob("*/*/Power.log"))
                except OSError:
                    pass
        return list(dict.fromkeys(path.resolve() for path in candidates))

    def resolve(self) -> Path | None:
        existing: list[tuple[int, Path]] = []
        for path in self.candidates():
            try:
                stat = path.stat()
                if stat_module.S_ISREG(stat.st_mode):
                    existing.append((stat.st_mtime_ns, path))
            except OSError:
                continue
        if not existing:
            return None
        return max(existing, key=lambda item: item[0])[1]


@dataclass(frozen=True, slots=True)
class TailBatch:
    lines: tuple[str, ...]
    path: Path | None
    bootstrap: bool = False
    source_reset: bool = False
    bootstrap_complete: bool = True


class PowerLogTailer:
    def __init__(self, locator: PowerLogLocator, *, initial_read_max_bytes: int = 64 * 1024 * 1024) -> None:
        self.locator = locator
        self.initial_read_max_bytes = max(1024 * 1024, int(initial_read_max_bytes))
        self.path: Path | None = None
        self.offset = 0
        self._partial = b""
        self._identity: tuple[int, int] | None = None
        self._discarding_long_line = False
        self._bootstrap_complete = False

    def reset(self) -> None:
        self.path = None
        self.offset = 0
        self._partial = b""
        self._identity = None
        self._discarding_long_line = False
        self._bootstrap_complete = False

    def poll(self, *, max_bytes: int = 2 * 1024 * 1024) -> TailBatch:
        path = self.locator.resolve()
        if path is None:
            source_reset = self.path is not None
            self.reset()
            return TailBatch((), None, source_reset=source_reset, bootstrap_complete=False)

        if self.path != path:
            self.path = path
            self.offset = 0
            self._partial = b""
            self._discarding_long_line = False
            return self._bootstrap(path)

        try:
            stat = path.stat()
        except OSError:
            source_reset = self.path is not None
            self.reset()
            return TailBatch((), None, source_reset=source_reset, bootstrap_complete=False)

        identity = self._file_identity(stat)
        if self._identity is not None and identity != self._identity:
            self.offset = 0
            self._partial = b""
            self._discarding_long_line = False
            return self._bootstrap(path, source_reset=True)

        size = stat.st_size

        if size < self.offset:
            self.offset = 0
            self._partial = b""
            self._discarding_long_line = False
            return self._bootstrap(path, source_reset=True)
        if size == self.offset:
            return TailBatch((), path, bootstrap_complete=self._bootstrap_complete)

        try:
            with path.open("rb") as handle:
                handle.seek(self.offset)
                data = handle.read(max(1, int(max_bytes)))
                self.offset = handle.tell()
        except OSError:
            return TailBatch((), path, bootstrap_complete=self._bootstrap_complete)
        return TailBatch(
            self._decode_complete_lines(data),
            path,
            bootstrap_complete=self._bootstrap_complete,
        )

    def _bootstrap(self, path: Path, *, source_reset: bool = True) -> TailBatch:
        try:
            with path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                size = stat.st_size
                start = max(0, size - self.initial_read_max_bytes)
                handle.seek(start)
                data = handle.read()
                self.offset = handle.tell()
                self._identity = self._file_identity(stat)
        except OSError:
            self.reset()
            return TailBatch((), None, bootstrap_complete=False)

        if start > 0:
            newline = data.find(b"\n")
            data = data[newline + 1 :] if newline >= 0 else b""
        matches = list(_GAMESTATE_CREATE_RE.finditer(data))
        if not matches:
            matches = list(_BARE_CREATE_RE.finditer(data))
        marker = matches[-1].start() if matches else -1
        self._bootstrap_complete = start == 0 or marker >= 0
        if marker >= 0:
            line_start = data.rfind(b"\n", 0, marker)
            prefix = data[:marker]
            spectator_start = max(prefix.rfind(item) for item in _SPECTATOR_START_MARKERS)
            spectator_end = max(prefix.rfind(item) for item in _SPECTATOR_END_MARKERS)
            if spectator_start > spectator_end:
                line_start = data.rfind(b"\n", 0, spectator_start)
            data = data[line_start + 1 :]
        return TailBatch(
            self._decode_complete_lines(data),
            path,
            bootstrap=True,
            source_reset=source_reset,
            bootstrap_complete=self._bootstrap_complete,
        )

    @staticmethod
    def _file_identity(stat: os.stat_result) -> tuple[int, int]:
        return (int(stat.st_dev), int(stat.st_ino))

    def _decode_complete_lines(self, data: bytes, *, flush: bool = False) -> tuple[str, ...]:
        combined = self._partial + data
        complete: list[bytes] = []
        start = 0
        index = 0
        while index < len(combined):
            byte = combined[index]
            if byte == 10:  # LF
                complete.append(combined[start:index])
                start = index + 1
            elif byte == 13:  # CR or the first half of CRLF
                if index + 1 >= len(combined) and not flush:
                    break
                complete.append(combined[start:index])
                if index + 1 < len(combined) and combined[index + 1] == 10:
                    index += 1
                start = index + 1
            index += 1
        self._partial = combined[start:]
        if flush and self._partial:
            complete.append(self._partial.rstrip(b"\r\n"))
            self._partial = b""
        decoded: list[str] = []
        for chunk in complete:
            if self._discarding_long_line:
                self._discarding_long_line = False
                continue
            if len(chunk) <= MAX_LINE_BYTES:
                decoded.append(chunk.decode("utf-8", "replace"))
        if len(self._partial) > MAX_LINE_BYTES:
            self._partial = b""
            self._discarding_long_line = True
        if flush:
            self._discarding_long_line = False
        return tuple(decoded)
