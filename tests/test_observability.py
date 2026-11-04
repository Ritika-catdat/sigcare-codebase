import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from sigcare_quality import observability
from sigcare_quality.observability import JsonFormatter, log_context, submit_with_context


def _render(message, *args, exc_info=None, **extra):
    record = logging.LogRecord("sigcare.test", logging.INFO, __file__, 1, message, args, exc_info)
    record.__dict__.update(extra)
    return json.loads(JsonFormatter().format(record))


def test_json_line_has_context_and_extra_fields():
    with log_context(job_id="abc", source_key="2026/Nov/02/input/a.pdf"):
        entry = _render("done %s", "a.pdf", duration_s=1.5)
    assert entry["message"] == "done a.pdf"
    assert entry["level"] == "INFO"
    assert entry["job_id"] == "abc"
    assert entry["source_key"] == "2026/Nov/02/input/a.pdf"
    assert entry["duration_s"] == 1.5
    assert "service" in entry and "env" in entry


def test_context_is_removed_after_block_and_none_is_skipped():
    with log_context(job_id="abc", request_id=None):
        assert "request_id" not in _render("x")
    assert "job_id" not in _render("x")


def test_exception_is_logged_with_type_and_traceback():
    try:
        raise ValueError("bad page")
    except ValueError:
        import sys
        entry = _render("failed", exc_info=sys.exc_info())
    assert entry["error_type"] == "ValueError"
    assert entry["error"] == "bad page"
    assert "Traceback" in entry["traceback"]


def test_long_messages_are_truncated():
    entry = _render("x" * 5000)
    assert len(entry["message"]) < 2100
    assert entry["message"].endswith("[truncated]")


def test_context_follows_work_into_thread_pool():
    def current_job():
        return observability._CONTEXT.get().get("job_id")

    with ThreadPoolExecutor(max_workers=2) as pool, log_context(job_id="job-1"):
        assert submit_with_context(pool, current_job).result() == "job-1"
        # Plain submit does not carry the context; that is why the helper exists.
        assert pool.submit(current_job).result() is None


class _FakeCloudWatch:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def put_metric_data(self, **kwargs):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.calls.append(kwargs)


def _use_fake_cloudwatch(monkeypatch, fake):
    # No background thread in tests: values stay queued until flush().
    monkeypatch.setattr(observability._METRICS, "_ensure_sender", lambda: None)
    monkeypatch.setattr(observability._METRICS, "_client", fake)
    monkeypatch.setattr(observability._METRICS, "_warned", False)


@pytest.fixture
def metrics(monkeypatch):
    fake = _FakeCloudWatch()
    _use_fake_cloudwatch(monkeypatch, fake)
    return fake


def test_metrics_are_off_by_default(metrics, monkeypatch):
    monkeypatch.delenv("SIGCARE_METRICS_ENABLED", raising=False)
    observability.emit_metric("DocumentsFailed")
    observability._METRICS.flush()
    assert metrics.calls == []


def test_metrics_are_published_with_service_dimension(metrics, monkeypatch):
    monkeypatch.setenv("SIGCARE_METRICS_ENABLED", "true")
    monkeypatch.setenv("SIGCARE_METRICS_NAMESPACE", "SigCare/Staging")
    observability.emit_metrics({"DocumentsProcessed": 1, "DocumentsNeedingReview": 0})
    assert metrics.calls == []  # queued, not sent by the caller
    observability._METRICS.flush()
    (call,) = metrics.calls
    assert call["Namespace"] == "SigCare/Staging"
    assert {m["MetricName"] for m in call["MetricData"]} == {"DocumentsProcessed", "DocumentsNeedingReview"}
    assert call["MetricData"][0]["Dimensions"][0]["Name"] == "Service"


def test_metric_failures_never_raise(monkeypatch, caplog):
    monkeypatch.setenv("SIGCARE_METRICS_ENABLED", "true")
    _use_fake_cloudwatch(monkeypatch, _FakeCloudWatch(fail=True))
    with caplog.at_level(logging.WARNING, logger="sigcare_quality.observability"):
        for _ in range(2):
            observability.emit_metric("Heartbeat")
            observability._METRICS.flush()
    # Warned once, not on every call.
    assert len([r for r in caplog.records if "Could not publish" in r.getMessage()]) == 1


def test_api_returns_request_id_header():
    from fastapi.testclient import TestClient

    from sigcare_quality import api

    client = TestClient(api.app)
    assert len(client.get("/health").headers["X-Request-ID"]) == 32
    echoed = client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert echoed.headers["X-Request-ID"] == "trace-123"
    # Unsafe values are replaced rather than copied into logs.
    replaced = client.get("/health", headers={"X-Request-ID": "has spaces; DROP"})
    assert replaced.headers["X-Request-ID"] != "has spaces; DROP"
