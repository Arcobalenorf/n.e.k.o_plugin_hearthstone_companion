from __future__ import annotations

import asyncio
import copy
import threading
import time
from concurrent.futures import Future, wait
from typing import Any, Awaitable, Callable

WriteCallback = Callable[[dict[str, Any]], Awaitable[Any]]
WriteSuccessCallback = Callable[[], None]


def _is_error_result(result: Any) -> bool:
    checker = getattr(result, "is_err", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return True
    return type(result).__name__ == "Err"


class AsyncStoreWriter:
    """Own a durable asyncio loop for writes submitted by the log thread."""

    def __init__(self, write: WriteCallback, logger: Any) -> None:
        self._write = write
        self._logger = logger
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopping_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._pending: set[Future[Any]] = set()
        self._write_lock: asyncio.Lock | None = None
        self._accepting = False
        self._write_failed = False
        self._last_error_code = ""
        self._next_write_sequence = 0
        self._health_sequence = 0

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._ready = threading.Event()
            self._write_failed = False
            self._last_error_code = ""
            self._next_write_sequence = 0
            self._health_sequence = 0
            self._stopping_thread = None
            thread = threading.Thread(target=self._run, name="hearthstone-store", daemon=True)
            self._thread = thread
            thread.start()
        if not self._ready.wait(3.0):
            raise RuntimeError("statistics store loop did not start")
        with self._lock:
            if (
                self._thread is not thread
                or self._stopping_thread is thread
                or not thread.is_alive()
            ):
                return False
            self._accepting = True
        return True

    def submit(
        self,
        value: dict[str, Any],
        *,
        on_success: WriteSuccessCallback | None = None,
    ) -> bool:
        return self._schedule(value, on_success=on_success) is not None

    def write_and_wait(self, value: dict[str, Any], timeout: float = 5.0) -> bool:
        future = self._schedule(value)
        if future is None:
            return False
        try:
            result = future.result(timeout=max(0.0, timeout))
        except Exception:
            return False
        return not _is_error_result(result)

    def _schedule(
        self,
        value: dict[str, Any],
        *,
        on_success: WriteSuccessCallback | None = None,
    ) -> Future[Any] | None:
        copied = copy.deepcopy(value)
        with self._lock:
            loop = self._loop
            thread = self._thread
            if not self._accepting or loop is None or thread is None or not thread.is_alive():
                self._next_write_sequence += 1
                self._record_failure_locked(
                    self._next_write_sequence,
                    "stats:writer_unavailable",
                )
                return None
            self._next_write_sequence += 1
            sequence = self._next_write_sequence
            coroutine = self._write_serial(copied, sequence, on_success=on_success)
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except Exception as exc:
                coroutine.close()
                self._record_failure_locked(sequence, f"stats:schedule:{type(exc).__name__}")
                return None
            self._pending.add(future)
        future.add_done_callback(self._completed)
        return future

    def flush(self, timeout: float = 5.0) -> bool:
        with self._lock:
            pending = set(self._pending)
        unfinished: set[Future[Any]] = set()
        if pending:
            _, unfinished = wait(pending, timeout=max(0.0, timeout))
        with self._lock:
            failed = self._write_failed
        return not unfinished and not failed

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread is not None and self._thread.is_alive())

    def is_accepting(self) -> bool:
        with self._lock:
            return bool(
                self._accepting
                and self._loop is not None
                and self._loop.is_running()
                and self._thread is not None
                and self._thread.is_alive()
            )

    def last_error_code(self) -> str:
        with self._lock:
            return self._last_error_code

    def stop(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._accepting = False
            loop = self._loop
            thread = self._thread
            signal_stop = thread is not None and self._stopping_thread is not thread
            if signal_stop:
                self._stopping_thread = thread
        flushed = self.flush(max(0.0, deadline - time.monotonic()))
        if signal_stop and loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        stopped = thread is None or not thread.is_alive()
        with self._lock:
            if stopped and self._stopping_thread is thread:
                self._stopping_thread = None
            if stopped and self._thread is thread:
                self._thread = None
                self._loop = None
                self._write_lock = None
            failed = self._write_failed
        return flushed and stopped and not failed

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        thread = threading.current_thread()
        with self._lock:
            self._loop = loop
            self._write_lock = asyncio.Lock()
            stop_requested = self._stopping_thread is thread
        try:
            if stop_requested:
                self._ready.set()
            else:
                with self._lock:
                    stop_requested = self._stopping_thread is thread
                if stop_requested:
                    self._ready.set()
                else:
                    loop.call_soon(self._ready.set)
                    loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()

    async def _write_serial(
        self,
        value: dict[str, Any],
        sequence: int,
        *,
        on_success: WriteSuccessCallback | None = None,
    ) -> Any:
        lock = self._write_lock
        if lock is None:
            raise RuntimeError("statistics store lock is unavailable")
        async with lock:
            try:
                result = await self._write(value)
            except Exception as exc:
                with self._lock:
                    self._record_failure_locked(sequence, f"stats:{type(exc).__name__}")
                self._logger.warning("Battlegrounds statistics Store write failed code=%s", type(exc).__name__)
                raise
            if _is_error_result(result):
                with self._lock:
                    self._record_failure_locked(sequence, "stats:store_err")
                self._logger.warning("Battlegrounds statistics Store write returned Err")
            else:
                with self._lock:
                    if sequence >= self._health_sequence:
                        self._health_sequence = sequence
                        self._write_failed = False
                        self._last_error_code = ""
                if on_success is not None:
                    try:
                        on_success()
                    except Exception as exc:
                        self._logger.warning(
                            "Battlegrounds statistics Store success callback failed code=%s",
                            type(exc).__name__,
                        )
            return result

    def _record_failure_locked(self, sequence: int, error_code: str) -> None:
        if sequence >= self._health_sequence:
            self._health_sequence = sequence
            self._write_failed = True
            self._last_error_code = error_code

    def _completed(self, future: Future[Any]) -> None:
        with self._lock:
            self._pending.discard(future)
        try:
            future.result()
        except Exception:
            pass

__all__ = ["AsyncStoreWriter"]
