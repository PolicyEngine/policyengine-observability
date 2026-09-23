from __future__ import annotations

import queue
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TextIO

from .config import ObservabilityConfig
from .destinations import (
    DestinationBuildContext,
    LogDestinationStrategy,
    RecordWriter,
    StdoutLogDestination,
)
from .diagnostics import Diagnostics

DESTINATION_FAILURE_LIMIT = 3


@dataclass(slots=True)
class _InlineDestination:
    strategy: LogDestinationStrategy
    writer: RecordWriter
    failures: int = 0


@dataclass(slots=True)
class _WriterCleanup:
    name: str
    thread: threading.Thread
    timeout_reported: bool = False


class DeliveryManager:
    """Fan out provider-neutral records to independently isolated writers."""

    def __init__(
        self,
        config: ObservabilityConfig,
        diagnostics: Diagnostics,
        *,
        stdout: TextIO | None = None,
    ) -> None:
        self.config = config
        self.diagnostics = diagnostics
        self._stdout = stdout or sys.stdout
        self._inline: list[_InlineDestination] = []
        self._queued: list[_QueuedWriter] = []
        self._cleanup_lock = threading.Lock()
        self._cleanup_tasks: list[_WriterCleanup] = []
        self._configure()

    def _configure(self) -> None:
        strategies = self.config.logging.destinations
        if not strategies:
            strategies = (StdoutLogDestination(),)
            self.diagnostics.report(
                "logging.destination_config",
                "No log destination was configured; using standard output.",
            )
        context = DestinationBuildContext(stdout=lambda: self._stdout)
        for strategy in strategies:
            try:
                self._add_strategy(strategy, context)
            except Exception as exc:
                self.diagnostics.report(
                    "logging.destination_config",
                    exc,
                    destination=getattr(strategy, "name", None),
                )
        if not self._inline and not self._queued:
            self._add_strategy(StdoutLogDestination(), context)

    def _add_strategy(
        self,
        strategy: LogDestinationStrategy,
        context: DestinationBuildContext,
    ) -> None:
        if strategy.delivery == "inline":
            self._inline.append(
                _InlineDestination(strategy, strategy.build_writer(context))
            )
            return
        if strategy.delivery != "queued":
            raise ValueError(
                f"Unsupported delivery mode for {strategy.name!r}: "
                f"{strategy.delivery!r}"
            )
        self._queued.append(
            _QueuedWriter(
                strategy,
                context,
                self.diagnostics,
            )
        )

    def emit(self, record: dict[str, Any]) -> None:
        disabled: list[_InlineDestination] = []
        for destination in tuple(self._inline):
            try:
                destination.writer.write(deepcopy(record))
                destination.failures = 0
            except Exception as exc:
                destination.failures += 1
                self.diagnostics.increment("logs.export_failure")
                self.diagnostics.report(
                    "logging.export",
                    exc,
                    destination=destination.strategy.name,
                    consecutive_failures=destination.failures,
                )
                if destination.failures >= DESTINATION_FAILURE_LIMIT:
                    disabled.append(destination)
        for destination in disabled:
            self._disable_inline(destination)
        for destination in tuple(self._queued):
            destination.enqueue(deepcopy(record))

    def _disable_inline(self, destination: _InlineDestination) -> None:
        try:
            self._inline.remove(destination)
        except ValueError:
            return
        self._start_writer_cleanup(
            destination.writer,
            destination.strategy.name,
        )
        self.diagnostics.report(
            "logging.destination_disabled",
            RuntimeError(
                "Log destination disabled after repeated write failures."
            ),
            destination=destination.strategy.name,
        )
        if self._inline or self._queued:
            return
        try:
            context = DestinationBuildContext(stdout=lambda: self._stdout)
            fallback = StdoutLogDestination()
            self._inline.append(
                _InlineDestination(fallback, fallback.build_writer(context))
            )
        except Exception as exc:
            self.diagnostics.report("logging.stdout_fallback", exc)

    def close(self, timeout_seconds: float | None = None) -> None:
        timeout = (
            self.config.logging.shutdown_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        try:
            timeout = min(max(float(timeout), 0.0), 30.0)
        except (TypeError, ValueError):
            timeout = 2.0
        deadline = time.monotonic() + timeout
        inline = tuple(self._inline)
        self._inline.clear()
        for destination in inline:
            self._start_writer_cleanup(
                destination.writer,
                destination.strategy.name,
            )
        for destination in tuple(self._queued):
            destination.close(max(0.0, deadline - time.monotonic()))
        with self._cleanup_lock:
            cleanup_tasks = tuple(self._cleanup_tasks)
        for task in cleanup_tasks:
            task.thread.join(max(0.0, deadline - time.monotonic()))
            should_report_timeout = False
            with self._cleanup_lock:
                if task.thread.is_alive() and not task.timeout_reported:
                    task.timeout_reported = True
                    should_report_timeout = True
            if should_report_timeout:
                self.diagnostics.increment("logs.shutdown_timeout")
                self.diagnostics.report(
                    "logging.shutdown_timeout",
                    "Log writer cleanup did not stop before its deadline.",
                    destination=task.name,
                )

    def _start_writer_cleanup(self, writer: RecordWriter, name: str) -> None:
        close = getattr(writer, "close", None)
        if not callable(close):
            return

        def run() -> None:
            try:
                close()
            except Exception as exc:
                self.diagnostics.report(
                    "logging.writer_close", exc, destination=name
                )

        task = _WriterCleanup(
            name=name,
            thread=threading.Thread(
                target=run,
                name=f"policyengine-observability-close-{name}",
                daemon=True,
            ),
        )
        try:
            with self._cleanup_lock:
                task.thread.start()
                self._cleanup_tasks.append(task)
        except Exception as exc:
            self.diagnostics.report(
                "logging.writer_close", exc, destination=name
            )

    @property
    def remote_enabled(self) -> bool:
        return bool(self._queued)

    @property
    def queue_depth(self) -> int:
        return sum(destination.queue_depth for destination in self._queued)


class _QueuedWriter:
    _STOP = object()

    def __init__(
        self,
        strategy: LogDestinationStrategy,
        context: DestinationBuildContext,
        diagnostics: Diagnostics,
    ) -> None:
        self.strategy = strategy
        self.context = context
        self.diagnostics = diagnostics
        capacity = max(
            1,
            min(int(getattr(strategy, "queue_capacity", 1_000)), 100_000),
        )
        self._queue: queue.Queue[dict[str, Any] | object] = queue.Queue(
            maxsize=capacity
        )
        self._closed = threading.Event()
        self._writer: RecordWriter | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"policyengine-observability-{strategy.name}",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, record: dict[str, Any]) -> None:
        if self._closed.is_set():
            self.diagnostics.increment("logs.dropped.closed")
            return
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self.diagnostics.increment("logs.dropped.queue_full")
            self.diagnostics.report(
                "logging.queue_full",
                "Remote log queue is full; newest record dropped.",
                destination=self.strategy.name,
                capacity=self._queue.maxsize,
            )

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def close(self, timeout_seconds: float) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._queue.put_nowait(self._STOP)
        except queue.Full:
            pass
        self._thread.join(max(0.0, timeout_seconds))
        if self._thread.is_alive():
            self.diagnostics.increment("logs.shutdown_timeout")
            self.diagnostics.report(
                "logging.shutdown_timeout",
                "Remote log worker did not stop before its deadline.",
                destination=self.strategy.name,
            )

    def _run(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    if self._closed.is_set():
                        return
                    continue
                consumed = [item]
                stop_after_batch = item is self._STOP
                batch: list[dict[str, Any]] = []
                if isinstance(item, dict):
                    batch.append(item)
                batch_size = max(
                    1,
                    min(
                        int(getattr(self.strategy, "batch_size", 1)),
                        10_000,
                    ),
                )
                while not stop_after_batch and len(batch) < batch_size:
                    try:
                        next_item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    consumed.append(next_item)
                    if next_item is self._STOP:
                        stop_after_batch = True
                    elif isinstance(next_item, dict):
                        batch.append(next_item)
                try:
                    if not batch:
                        return
                    if self._writer is None:
                        self._writer = self.strategy.build_writer(self.context)
                    write_many = getattr(self._writer, "write_many", None)
                    if callable(write_many):
                        write_many(batch)
                    else:
                        for record in batch:
                            self._writer.write(record)
                except Exception as exc:
                    self.diagnostics.increment("logs.export_failure")
                    self.diagnostics.report(
                        "logging.export",
                        exc,
                        destination=self.strategy.name,
                    )
                finally:
                    for _item in consumed:
                        self._queue.task_done()
                if stop_after_batch:
                    return
        finally:
            if self._writer is not None:
                close = getattr(self._writer, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:
                        self.diagnostics.report(
                            "logging.writer_close",
                            exc,
                            destination=self.strategy.name,
                        )
