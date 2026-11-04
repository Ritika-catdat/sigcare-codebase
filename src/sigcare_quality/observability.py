"""Logging and metrics shared by the API and the S3 worker.

Logs
    One JSON object per line on stdout (SIGCARE_LOG_FORMAT=json, the default)
    or readable text (SIGCARE_LOG_FORMAT=text, for local runs). Docker ships
    stdout to CloudWatch Logs; see docker-compose.cloudwatch.yml.

    Every line carries the service and environment, plus the current job
    context (job_id, source_key, request_id) set with log_context(). The
    context lives in contextvars, so it follows the job through the code; use
    submit_with_context() when handing work to a thread pool.

    Never log prescription contents (patient names, DOB, medication). Log
    identifiers, counts, stages and timings only.

Metrics
    emit_metric() sends custom CloudWatch metrics to the SigCare/<Environment>
    namespace when SIGCARE_METRICS_ENABLED=true. Values are queued and sent in
    batches by a background thread, so callers never wait on CloudWatch.
    Failures are logged and swallowed: monitoring must never break processing.
"""
from __future__ import annotations

import atexit
import contextlib
import contextvars
import json
import logging
import os
import queue
import sys
import threading
import traceback
from concurrent.futures import Executor, Future
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

# Context fields added to every log line while set.
_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "sigcare_log_context", default={}
)

# Attributes every LogRecord has; anything else passed via extra= is logged as a field.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message", "asctime", "taskName",
    "color_message",  # uvicorn's copy of the message with terminal colour codes
}
# Long exception messages can echo request or document content; keep them short.
_MAX_FIELD_CHARS = 2000

SERVICE = os.getenv("SIGCARE_SERVICE_NAME", "sigcare")
ENVIRONMENT = os.getenv("APP_ENV", "local")


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + "...[truncated]"
    return value


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object (CloudWatch Logs Insights friendly)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": _truncate(record.getMessage()),
            "service": SERVICE,
            "env": ENVIRONMENT,
            "thread": record.threadName,
        }
        entry.update(_CONTEXT.get())
        for key, value in vars(record).items():
            if key not in _RECORD_ATTRS and not key.startswith("_"):
                entry[key] = _truncate(value)
        if record.exc_info:
            exc_type, exc, _ = record.exc_info
            entry["error_type"] = exc_type.__name__ if exc_type else None
            entry["error"] = _truncate(str(exc))
            entry["traceback"] = _truncate("".join(traceback.format_exception(*record.exc_info)))
        return json.dumps(entry, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable lines for local runs; context fields are appended as key=value."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        context = dict(_CONTEXT.get())
        context.update({k: v for k, v in vars(record).items()
                        if k not in _RECORD_ATTRS and not k.startswith("_")})
        if context:
            line += " | " + " ".join(f"{k}={v}" for k, v in context.items())
        return line


class _DropHealthChecks(logging.Filter):
    """Docker probes /health every 30s; those request lines are noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "path", None) not in ("/health",)


def configure_logging(service: str) -> None:
    """Send all logs (ours, uvicorn's, boto's) to stdout in one format. Safe to call twice."""
    global SERVICE
    SERVICE = os.getenv("SIGCARE_SERVICE_NAME", service)

    handler = logging.StreamHandler(sys.stdout)
    use_text = os.getenv("SIGCARE_LOG_FORMAT", "json").strip().lower() == "text"
    handler.setFormatter(TextFormatter() if use_text else JsonFormatter())
    handler.addFilter(_DropHealthChecks())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(os.getenv("SIGCARE_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")).strip().upper())

    # uvicorn installs its own handlers before importing the app; route them
    # through ours. Its access log is replaced by the API's request log.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # AWS SDK debug output is very verbose and can include request details.
    for name in ("boto3", "botocore", "s3transfer", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add fields (job_id, source_key, ...) to every log line inside the block."""
    token = _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def submit_with_context(executor: Executor, fn: Callable[..., Any], /, *args: Any,
                        **kwargs: Any) -> Future:
    """executor.submit() that keeps the caller's log context in the worker thread."""
    return executor.submit(contextvars.copy_context().run, fn, *args, **kwargs)


# ------------------------------- Metrics -------------------------------

class _Metrics:
    """Failure-tolerant, non-blocking publisher for CloudWatch PutMetricData.

    emit() only puts values on a bounded queue. A daemon thread sends them in
    batches; if CloudWatch is slow or unreachable the queue fills and new
    values are dropped rather than slowing down document processing.
    """

    _BATCH = 500          # PutMetricData accepts up to 1000 values per request
    _QUEUE_SIZE = 10_000

    def __init__(self) -> None:
        self._client: Any = None
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._QUEUE_SIZE)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._warned = False

    @property
    def enabled(self) -> bool:
        return os.getenv("SIGCARE_METRICS_ENABLED", "false").strip().lower() == "true"

    @property
    def namespace(self) -> str:
        return os.getenv("SIGCARE_METRICS_NAMESPACE", f"SigCare/{ENVIRONMENT.title()}")

    def _cloudwatch(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "cloudwatch",
                region_name=os.getenv("AWS_REGION", "eu-west-2"),
                config=Config(connect_timeout=2, read_timeout=5,
                              retries={"mode": "standard", "max_attempts": 3}),
            )
        return self._client

    def _ensure_sender(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="sigcare-metrics",
                                                daemon=True)
                self._thread.start()

    def emit(self, values: dict[str, float], unit: str = "Count") -> None:
        if not self.enabled or not values:
            return
        self._ensure_sender()
        dimensions = [{"Name": "Service", "Value": SERVICE}]
        timestamp = datetime.now(timezone.utc)
        for name, value in values.items():
            try:
                self._queue.put_nowait({"MetricName": name, "Value": float(value), "Unit": unit,
                                        "Dimensions": dimensions, "Timestamp": timestamp})
            except queue.Full:
                logging.getLogger(__name__).debug("Metrics queue full; dropped %s", name)

    def _drain(self, first: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        batch = [first] if first else []
        while len(batch) < self._BATCH:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _send(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            self._cloudwatch().put_metric_data(Namespace=self.namespace, MetricData=batch)
        except Exception as exc:
            # Warn once with the cause, then only at debug level to avoid log floods.
            log = logging.getLogger(__name__)
            if not self._warned:
                log.warning("Could not publish CloudWatch metrics: %s: %s",
                            type(exc).__name__, exc)
                self._warned = True
            else:
                log.debug("Could not publish CloudWatch metrics", exc_info=True)
        else:
            self._warned = False

    def _run(self) -> None:
        while True:
            self._send(self._drain(self._queue.get()))

    def flush(self) -> None:
        """Send whatever is queued now (used at exit and in tests)."""
        while not self._queue.empty():
            self._send(self._drain())


_METRICS = _Metrics()
# Send what is still queued when the process stops (e.g. docker stop).
atexit.register(_METRICS.flush)


def emit_metric(name: str, value: float = 1, unit: str = "Count") -> None:
    """Publish one metric value, e.g. emit_metric("DocumentsFailed")."""
    _METRICS.emit({name: value}, unit)


def emit_metrics(values: dict[str, float], unit: str = "Count") -> None:
    """Publish several metrics with the same unit in one request."""
    _METRICS.emit(values, unit)
