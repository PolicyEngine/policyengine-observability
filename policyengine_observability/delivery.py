from __future__ import annotations

import functools
import json
import queue
import sys
import threading
from collections.abc import Callable
from typing import Any, Protocol, TextIO

from .config import GoogleCloudLoggingConfig, ObservabilityConfig
from .diagnostics import Diagnostics


class RecordWriter(Protocol):
    def write(self, record: dict[str, Any]) -> None: ...

    def write_many(self, records: list[dict[str, Any]]) -> None: ...

    def close(self) -> None: ...


class DeliveryManager:
    def __init__(
        self,
        config: ObservabilityConfig,
        diagnostics: Diagnostics,
        *,
        stdout: TextIO | None = None,
        writer_factory: Callable[[GoogleCloudLoggingConfig], RecordWriter]
        | None = None,
    ) -> None:
        self.config = config
        self.diagnostics = diagnostics
        self._stdout = stdout or sys.stdout
        self._writer_factory = writer_factory or _GoogleCloudWriter
        self._remote: _QueuedWriter | None = None
        remote = config.logging.remote
        if (
            remote is not None
            and config.deployment.platform == "modal"
            and config.identity_complete
            and remote.project_id.strip()
            and remote.log_name.strip()
        ):
            self._remote = _QueuedWriter(
                remote,
                diagnostics,
                writer_factory=self._writer_factory,
            )

    def emit(self, record: dict[str, Any]) -> None:
        if self.config.logging.stdout_enabled:
            try:
                print(
                    json.dumps(
                        record,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ),
                    file=self._stdout,
                    flush=True,
                )
            except Exception as exc:
                self.diagnostics.report("stdout.write", exc)
        if self._remote is not None:
            self._remote.enqueue(record.copy())

    def close(self, timeout_seconds: float | None = None) -> None:
        if self._remote is None:
            return
        self._remote.close(timeout_seconds)

    @property
    def remote_enabled(self) -> bool:
        return self._remote is not None

    @property
    def queue_depth(self) -> int:
        return self._remote.queue_depth if self._remote else 0


class _QueuedWriter:
    _STOP = object()

    def __init__(
        self,
        config: GoogleCloudLoggingConfig,
        diagnostics: Diagnostics,
        *,
        writer_factory: Callable[[GoogleCloudLoggingConfig], RecordWriter],
    ) -> None:
        self.config = config
        self.diagnostics = diagnostics
        self._writer_factory = writer_factory
        self._queue: queue.Queue[dict[str, Any] | object] = queue.Queue(
            maxsize=max(1, min(config.queue_capacity, 100_000))
        )
        self._closed = threading.Event()
        self._writer: RecordWriter | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="policyengine-observability-log-writer",
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
                capacity=self._queue.maxsize,
            )

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def close(self, timeout_seconds: float | None = None) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._queue.put_nowait(self._STOP)
        except queue.Full:
            pass
        timeout = (
            self.config.close_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        self._thread.join(max(0.0, timeout))
        if self._thread.is_alive():
            self.diagnostics.increment("logs.shutdown_timeout")
            self.diagnostics.report(
                "logging.shutdown_timeout",
                "Remote log worker did not stop before its deadline.",
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
                while not stop_after_batch and len(batch) < max(
                    1, self.config.batch_size
                ):
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
                        self._writer = self._writer_factory(self.config)
                    write_many = getattr(self._writer, "write_many", None)
                    if callable(write_many):
                        write_many(batch)
                    else:
                        for record in batch:
                            self._writer.write(record)
                except Exception as exc:
                    self.diagnostics.increment("logs.export_failure")
                    self.diagnostics.report("logging.export", exc)
                finally:
                    for _consumed_item in consumed:
                        self._queue.task_done()
                if stop_after_batch:
                    return
        finally:
            if self._writer is not None:
                try:
                    self._writer.close()
                except Exception as exc:
                    self.diagnostics.report("logging.writer_close", exc)


class _GoogleCloudWriter:
    """Lazy worker-thread-only Cloud Logging client."""

    def __init__(self, config: GoogleCloudLoggingConfig) -> None:
        from google.cloud import logging_v2

        from .google_credentials import load_google_credentials

        credentials = load_google_credentials(prefer_workload_identity=True)
        self._client = logging_v2.Client(
            project=config.project_id,
            credentials=credentials,
        )
        self._logger = self._client.logger(config.log_name)
        self._timeout = max(0.1, min(config.write_timeout_seconds, 60.0))
        self._bound_write_timeout()
        logging_v2._instrumentation_emitted = True

    def write(self, record: dict[str, Any]) -> None:
        self.write_many([record])

    def write_many(self, records: list[dict[str, Any]]) -> None:
        batch = self._logger.batch()
        for record in records:
            kwargs = self._entry_kwargs(record)
            batch.log_struct(record, **kwargs)
        batch.commit()

    def _entry_kwargs(self, record: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "severity": record.get("severity", "DEFAULT"),
        }
        trace = record.get("logging.googleapis.com/trace")
        span_id = record.get("logging.googleapis.com/spanId")
        if trace:
            kwargs["trace"] = trace
        if span_id:
            kwargs["span_id"] = span_id
        kwargs["trace_sampled"] = bool(
            record.get("logging.googleapis.com/trace_sampled", False)
        )
        return kwargs

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def _bound_write_timeout(self) -> None:
        api = getattr(self._client, "logging_api", None)
        gapic = getattr(api, "_gapic_api", None)
        if gapic is None:
            return
        from google.api_core import exceptions as api_exceptions
        from google.api_core.retry import Retry, if_exception_type

        retry = Retry(
            initial=0.1,
            maximum=1.0,
            multiplier=1.3,
            timeout=self._timeout,
            predicate=if_exception_type(
                api_exceptions.DeadlineExceeded,
                api_exceptions.InternalServerError,
                api_exceptions.ServiceUnavailable,
            ),
        )
        gapic.write_log_entries = functools.partial(
            gapic.write_log_entries,
            retry=retry,
            timeout=self._timeout,
        )
