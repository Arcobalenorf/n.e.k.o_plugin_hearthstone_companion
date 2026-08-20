from __future__ import annotations

import asyncio
import threading
import time

from hearthstone_companion_under_test.store_writer import AsyncStoreWriter


class _Logger:
    def warning(self, *_args: object) -> None:
        pass


def test_store_writer_is_accepting_tracks_start_stop_request_and_completion() -> None:
    async def write(_value: dict[str, object]) -> bool:
        return True

    writer = AsyncStoreWriter(write, _Logger())
    assert writer.is_accepting() is False
    assert writer.start() is True
    assert writer.is_accepting() is True

    flush_entered = threading.Event()
    release_flush = threading.Event()
    stop_result: list[bool] = []

    def blocking_flush(_timeout: float = 5.0) -> bool:
        flush_entered.set()
        assert release_flush.wait(1.0)
        return True

    writer.flush = blocking_flush  # type: ignore[method-assign]
    stopping = threading.Thread(
        target=lambda: stop_result.append(writer.stop(timeout=1.0)),
        daemon=True,
    )
    stopping.start()
    assert flush_entered.wait(1.0)

    assert writer.is_running() is True
    assert writer.is_accepting() is False
    release_flush.set()
    stopping.join(1.0)

    assert stopping.is_alive() is False
    assert stop_result == [True]
    assert writer.is_running() is False
    assert writer.is_accepting() is False


def test_store_writer_start_waits_until_owner_loop_is_running(monkeypatch) -> None:
    run_entered = threading.Event()
    allow_running = threading.Event()
    stop_requested = threading.Event()
    start_returned = threading.Event()

    class ControlledLoop:
        def __init__(self) -> None:
            self.running = False
            self.callbacks: list[object] = []

        def call_soon(self, callback: object) -> None:
            self.callbacks.append(callback)

        def run_forever(self) -> None:
            run_entered.set()
            assert allow_running.wait(1.0)
            self.running = True
            callbacks, self.callbacks = self.callbacks, []
            for callback in callbacks:
                callback()  # type: ignore[operator]
            assert stop_requested.wait(1.0)
            self.running = False

        def is_running(self) -> bool:
            return self.running

        def is_closed(self) -> bool:
            return False

        def stop(self) -> None:
            stop_requested.set()

        def call_soon_threadsafe(self, callback: object) -> None:
            callback()  # type: ignore[operator]

        def close(self) -> None:
            return None

    loop = ControlledLoop()
    monkeypatch.setattr(asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(asyncio, "set_event_loop", lambda _loop: None)
    monkeypatch.setattr(asyncio, "all_tasks", lambda _loop: set())

    async def write(_value: dict[str, object]) -> bool:
        return True

    writer = AsyncStoreWriter(write, _Logger())
    start_result: list[bool] = []

    def start_writer() -> None:
        start_result.append(writer.start())
        start_returned.set()

    starting = threading.Thread(target=start_writer, daemon=True)
    starting.start()
    assert run_entered.wait(1.0)
    assert start_returned.wait(0.05) is False

    allow_running.set()
    assert start_returned.wait(1.0)
    assert start_result == [True]
    assert writer.is_accepting() is True
    assert writer.stop(timeout=1.0) is True
    starting.join(1.0)
    assert starting.is_alive() is False


def test_stop_before_owner_loop_publish_leaves_writer_nonaccepting_and_stoppable(
    monkeypatch,
) -> None:
    loop_factory_entered = threading.Event()
    release_loop_publish = threading.Event()
    real_new_event_loop = asyncio.new_event_loop

    def delayed_new_event_loop():  # type: ignore[no-untyped-def]
        loop_factory_entered.set()
        assert release_loop_publish.wait(1.0)
        return real_new_event_loop()

    monkeypatch.setattr(asyncio, "new_event_loop", delayed_new_event_loop)

    async def write(_value: dict[str, object]) -> bool:
        return True

    writer = AsyncStoreWriter(write, _Logger())
    start_result: list[bool] = []
    stop_result: list[bool] = []
    starting = threading.Thread(
        target=lambda: start_result.append(writer.start()),
        daemon=True,
    )
    starting.start()
    assert loop_factory_entered.wait(1.0)

    stopping = threading.Thread(
        target=lambda: stop_result.append(writer.stop(timeout=1.0)),
        daemon=True,
    )
    stopping.start()
    deadline = time.monotonic() + 1.0
    while writer._stopping_thread is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert writer._stopping_thread is writer._thread
    release_loop_publish.set()
    starting.join(1.0)
    stopping.join(1.0)

    assert starting.is_alive() is False
    assert stopping.is_alive() is False
    assert start_result == [False]
    assert stop_result == [True]
    assert writer.is_running() is False
    assert writer.is_accepting() is False
    restarted_at = time.monotonic()
    assert writer.stop(timeout=0.1) is True
    assert time.monotonic() - restarted_at < 0.2


def test_stop_after_loop_publish_before_run_forever_is_delivered(monkeypatch) -> None:
    run_forever_entered = threading.Event()
    release_run_forever = threading.Event()

    class GatedLoop:
        def __init__(self) -> None:
            self.running = False
            self.closed = False
            self.callbacks: list[object] = []

        def call_soon(self, callback: object) -> None:
            self.callbacks.append(callback)

        def call_soon_threadsafe(self, callback: object) -> None:
            self.callbacks.append(callback)

        def run_forever(self) -> None:
            run_forever_entered.set()
            assert release_run_forever.wait(1.0)
            self.running = True
            callbacks, self.callbacks = self.callbacks, []
            for callback in callbacks:
                callback()  # type: ignore[operator]
            self.running = False

        def is_running(self) -> bool:
            return self.running

        def is_closed(self) -> bool:
            return self.closed

        def stop(self) -> None:
            self.running = False

        def close(self) -> None:
            self.closed = True

    loop = GatedLoop()
    monkeypatch.setattr(asyncio, "new_event_loop", lambda: loop)
    monkeypatch.setattr(asyncio, "set_event_loop", lambda _loop: None)
    monkeypatch.setattr(asyncio, "all_tasks", lambda _loop: set())

    async def write(_value: dict[str, object]) -> bool:
        return True

    writer = AsyncStoreWriter(write, _Logger())
    start_result: list[bool] = []
    stop_result: list[bool] = []
    starting = threading.Thread(
        target=lambda: start_result.append(writer.start()),
        daemon=True,
    )
    starting.start()
    assert run_forever_entered.wait(1.0)

    stopping = threading.Thread(
        target=lambda: stop_result.append(writer.stop(timeout=1.0)),
        daemon=True,
    )
    stopping.start()
    deadline = time.monotonic() + 1.0
    while writer._stopping_thread is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert writer._stopping_thread is writer._thread
    release_run_forever.set()
    starting.join(1.0)
    stopping.join(1.0)

    assert starting.is_alive() is False
    assert stopping.is_alive() is False
    assert start_result == [False]
    assert stop_result == [True]
    assert writer.is_running() is False
    assert writer.is_accepting() is False
    assert loop.closed is True


def test_store_writer_uses_long_lived_owner_loop_and_flushes() -> None:
    writes: list[tuple[int, dict[str, object]]] = []

    async def write(value: dict[str, object]) -> bool:
        await asyncio.sleep(0)
        writes.append((threading.get_ident(), value))
        return True

    caller_thread = threading.get_ident()
    writer = AsyncStoreWriter(write, _Logger())
    assert writer.start() is True
    assert writer.start() is False
    assert writer.submit({"games": 1}) is True
    assert writer.submit({"games": 2}) is True
    assert writer.stop() is True

    assert [value for _, value in writes] == [{"games": 1}, {"games": 2}]
    assert all(thread_id != caller_thread for thread_id, _ in writes)
    assert writer.submit({"games": 3}) is False


def test_store_writer_serializes_slow_older_write_before_newer_state() -> None:
    writes: list[int] = []

    async def write(value: dict[str, object]) -> bool:
        games = int(value["games"])
        if games == 1:
            await asyncio.sleep(0.03)
        writes.append(games)
        return True

    writer = AsyncStoreWriter(write, _Logger())
    writer.start()
    assert writer.submit({"games": 1}) is True
    assert writer.submit({"games": 2}) is True
    assert writer.stop() is True

    assert writes == [1, 2]


def test_write_and_wait_reports_store_error_result() -> None:
    class Err:
        pass

    async def write(_value: dict[str, object]) -> object:
        return Err()

    writer = AsyncStoreWriter(write, _Logger())
    writer.start()
    assert writer.write_and_wait({"games": 1}) is False
    assert writer.last_error_code() == "stats:store_err"
    assert writer.stop() is False


def test_submit_error_makes_flush_and_stop_report_failure() -> None:
    class Err:
        pass

    async def write(_value: dict[str, object]) -> object:
        return Err()

    writer = AsyncStoreWriter(write, _Logger())
    writer.start()
    assert writer.submit({"games": 1}) is True

    assert writer.flush() is False
    assert writer.stop() is False
    assert writer.is_running() is False


def test_successful_write_recovers_from_previous_store_error() -> None:
    class Err:
        pass

    attempts = 0

    async def write(_value: dict[str, object]) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return Err()
        return True

    writer = AsyncStoreWriter(write, _Logger())
    writer.start()

    assert writer.write_and_wait({"games": 1}) is False
    assert writer.last_error_code() == "stats:store_err"
    assert writer.flush() is False

    assert writer.write_and_wait({"games": 2}) is True
    assert writer.last_error_code() == ""
    assert writer.flush() is True
    assert writer.stop() is True


def test_submit_success_callback_requires_confirmed_store_success() -> None:
    class Err:
        pass

    attempts = 0
    confirmed: list[str] = []

    async def write(_value: dict[str, object]) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return Err()
        if attempts == 2:
            raise OSError("store unavailable")
        return True

    writer = AsyncStoreWriter(write, _Logger())
    writer.start()

    assert writer.submit({"games": 1}, on_success=lambda: confirmed.append("err")) is True
    assert writer.flush() is False
    assert confirmed == []

    assert writer.submit({"games": 2}, on_success=lambda: confirmed.append("exception")) is True
    assert writer.flush() is False
    assert confirmed == []

    assert writer.submit({"games": 3}, on_success=lambda: confirmed.append("success")) is True
    assert writer.flush() is True
    assert confirmed == ["success"]
    assert writer.stop() is True


def test_successful_write_recovers_from_unavailable_submission() -> None:
    async def write(_value: dict[str, object]) -> bool:
        return True

    writer = AsyncStoreWriter(write, _Logger())
    writer.start()
    with writer._lock:
        writer._accepting = False

    assert writer.submit({"games": 1}) is False
    assert writer.last_error_code() == "stats:writer_unavailable"

    with writer._lock:
        writer._accepting = True
    assert writer.write_and_wait({"games": 2}) is True
    assert writer.last_error_code() == ""
    assert writer.stop() is True


def test_newer_queued_snapshot_recovers_flush_and_stop_from_older_failure() -> None:
    class Err:
        pass

    async def write(value: dict[str, object]) -> object:
        if value["games"] == 1:
            return Err()
        return True

    writer = AsyncStoreWriter(write, _Logger())
    writer.start()
    assert writer.submit({"games": 1}) is True
    assert writer.submit({"games": 2}) is True

    assert writer.flush() is True
    assert writer.last_error_code() == ""
    assert writer.stop() is True


def test_older_success_does_not_hide_newer_schedule_failure(monkeypatch) -> None:
    write_started = threading.Event()
    release_write = threading.Event()
    fail_next_schedule = False
    original_schedule = asyncio.run_coroutine_threadsafe

    async def write(value: dict[str, object]) -> bool:
        if value["games"] == 1:
            write_started.set()
            await asyncio.to_thread(release_write.wait)
        return True

    def controlled_schedule(coroutine, loop):  # type: ignore[no-untyped-def]
        nonlocal fail_next_schedule
        if fail_next_schedule:
            fail_next_schedule = False
            raise RuntimeError("schedule unavailable")
        return original_schedule(coroutine, loop)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", controlled_schedule)
    writer = AsyncStoreWriter(write, _Logger())
    writer.start()
    assert writer.submit({"games": 1}) is True
    assert write_started.wait(1.0)

    fail_next_schedule = True
    assert writer.submit({"games": 2}) is False
    assert writer.last_error_code() == "stats:schedule:RuntimeError"
    release_write.set()

    assert writer.flush() is False
    assert writer.last_error_code() == "stats:schedule:RuntimeError"

    assert writer.write_and_wait({"games": 3}) is True
    assert writer.last_error_code() == ""
    assert writer.stop() is True


def test_timed_out_write_can_be_compensated_in_serial_order() -> None:
    release = threading.Event()
    writes: list[int] = []

    async def write(value: dict[str, object]) -> bool:
        games = int(value["games"])
        if games == 0:
            await asyncio.to_thread(release.wait)
        writes.append(games)
        return True

    writer = AsyncStoreWriter(write, _Logger())
    writer.start()
    assert writer.write_and_wait({"games": 0}, timeout=0.1) is False
    assert writer.submit({"games": 7}) is True
    release.set()

    assert writer.stop() is True
    assert writes == [0, 7]


def test_concurrent_submit_is_registered_before_stop_closes_the_loop(monkeypatch) -> None:
    writes: list[int] = []
    schedule_entered = threading.Event()
    release_schedule = threading.Event()
    stop_returned = threading.Event()
    original_schedule = asyncio.run_coroutine_threadsafe

    async def write(value: dict[str, object]) -> bool:
        writes.append(int(value["games"]))
        return True

    def controlled_schedule(coroutine, loop):  # type: ignore[no-untyped-def]
        schedule_entered.set()
        release_schedule.wait(1.0)
        return original_schedule(coroutine, loop)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", controlled_schedule)
    writer = AsyncStoreWriter(write, _Logger())
    assert writer.start() is True
    submit_result: list[bool] = []
    stop_result: list[bool] = []
    submit_thread = threading.Thread(target=lambda: submit_result.append(writer.submit({"games": 9})))
    submit_thread.start()
    assert schedule_entered.wait(1.0)

    def stop_writer() -> None:
        stop_result.append(writer.stop(timeout=1.0))
        stop_returned.set()

    stop_thread = threading.Thread(target=stop_writer)
    stop_thread.start()
    assert stop_returned.wait(0.05) is False
    release_schedule.set()
    submit_thread.join(1.0)
    stop_thread.join(1.0)

    assert submit_thread.is_alive() is False
    assert stop_thread.is_alive() is False
    assert submit_result == [True]
    assert stop_result == [True]
    assert writes == [9]


def test_old_stop_cannot_clear_new_writer_generation() -> None:
    old_final_check_entered = threading.Event()
    new_start_published = threading.Event()
    writes: list[int] = []

    async def write(value: dict[str, object]) -> bool:
        writes.append(int(value["games"]))
        return True

    class OldLoop:
        def is_running(self) -> bool:
            return True

        def is_closed(self) -> bool:
            return False

        def stop(self) -> None:
            return None

        def call_soon_threadsafe(self, _callback: object) -> None:
            return None

    class OldThread:
        def __init__(self) -> None:
            self.alive = True
            self.checks = 0
            self.lock = threading.Lock()

        def is_alive(self) -> bool:
            with self.lock:
                self.checks += 1
                check = self.checks
            if check == 2:
                old_final_check_entered.set()
                assert new_start_published.wait(1.0)
            return self.alive

        def join(self, timeout: float | None = None) -> None:
            _ = timeout
            self.alive = False

    writer = AsyncStoreWriter(write, _Logger())
    old_thread = OldThread()
    writer._thread = old_thread  # type: ignore[assignment]
    writer._loop = OldLoop()  # type: ignore[assignment]
    writer._accepting = True
    old_stop_result: list[bool] = []
    old_stopping = threading.Thread(
        target=lambda: old_stop_result.append(writer.stop(timeout=1.0)),
        daemon=True,
    )
    old_stopping.start()
    assert old_final_check_entered.wait(1.0)

    assert writer.start() is True
    new_thread = writer._thread
    assert new_thread is not None and new_thread is not old_thread
    new_start_published.set()
    old_stopping.join(1.0)

    assert old_stopping.is_alive() is False
    assert old_stop_result == [True]
    assert writer._thread is new_thread
    assert writer.is_accepting() is True
    assert writer.write_and_wait({"games": 12}) is True
    assert writes == [12]
    assert writer.stop(timeout=1.0) is True


def test_old_stop_cannot_capture_new_writer_generation_after_flush() -> None:
    flush_entered = threading.Event()
    release_flush = threading.Event()
    writes: list[int] = []

    async def write(value: dict[str, object]) -> bool:
        writes.append(int(value["games"]))
        return True

    class DeadThread:
        def is_alive(self) -> bool:
            return False

    writer = AsyncStoreWriter(write, _Logger())
    writer._thread = DeadThread()  # type: ignore[assignment]
    writer._accepting = True
    original_flush = writer.flush

    def blocking_flush(_timeout: float = 5.0) -> bool:
        flush_entered.set()
        assert release_flush.wait(1.0)
        return True

    writer.flush = blocking_flush  # type: ignore[method-assign]
    old_stop_result: list[bool] = []
    old_stopping = threading.Thread(
        target=lambda: old_stop_result.append(writer.stop(timeout=1.0)),
        daemon=True,
    )
    old_stopping.start()
    assert flush_entered.wait(1.0)

    assert writer.start() is True
    new_thread = writer._thread
    assert new_thread is not None
    assert writer.is_accepting() is True
    release_flush.set()
    old_stopping.join(1.0)

    assert old_stopping.is_alive() is False
    assert old_stop_result == [True]
    assert writer._thread is new_thread
    assert writer.is_accepting() is True
    writer.flush = original_flush  # type: ignore[method-assign]
    assert writer.write_and_wait({"games": 13}) is True
    assert writes == [13]
    assert writer.stop(timeout=1.0) is True


def test_repeated_stop_does_not_interrupt_writer_loop_cancellation_cleanup() -> None:
    write_started = threading.Event()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()

    async def write(_value: dict[str, object]) -> bool:
        write_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            while not release_cleanup.is_set():
                await asyncio.sleep(0.005)
            cleanup_finished.set()
        return True

    writer = AsyncStoreWriter(write, _Logger())
    assert writer.start() is True
    assert writer.submit({"games": 1}) is True
    assert write_started.wait(1.0)
    writer.flush = lambda _timeout=5.0: False  # type: ignore[method-assign]
    first_stop_result: list[bool] = []
    first_stopping = threading.Thread(
        target=lambda: first_stop_result.append(writer.stop(timeout=1.0)),
        daemon=True,
    )
    first_stopping.start()
    assert cleanup_entered.wait(1.0)

    assert writer.stop(timeout=0.05) is False
    assert writer.is_accepting() is False
    release_cleanup.set()
    first_stopping.join(1.0)

    assert first_stopping.is_alive() is False
    assert first_stop_result == [False]
    assert cleanup_finished.is_set()
    assert writer.is_running() is False
    assert writer.is_accepting() is False
