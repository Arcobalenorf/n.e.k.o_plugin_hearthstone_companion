from __future__ import annotations

import asyncio
import threading

from hearthstone_companion_under_test.store_writer import AsyncStoreWriter


class _Logger:
    def warning(self, *_args: object) -> None:
        pass


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
