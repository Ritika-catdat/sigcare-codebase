import json
import time

import jwt
import pytest
import requests

from sigcare_quality import processing
from sigcare_quality.intake_client import (
    DELIVERED, DISABLED, DUPLICATE, FAILED, IntakeClient, IntakeConfig,
)
from sigcare_quality.s3_watcher import S3Watcher, WatcherConfig

from test_s3_watcher import DAY, INTER, FakeS3, _pdf_bytes

SECRET = "s" * 64
URL = "https://intake.example.test/api/sigcare/intakes"
RESULT = {"job_id": "185c563d751ac337040ebe2e810cacf0", "status": "COMPLETED",
          "results": [{"prescription_number": 1, "prescription": {"last_name": "Bradley"}}]}


class FakeResponse:
    def __init__(self, http_status=200, body=None, headers=None):
        self.status_code = http_status
        self._body = body
        self.reason = "OK"
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakeSession:
    """Plays back responses (or raises exceptions) and records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, data, headers, timeout, allow_redirects):
        self.requests.append({"url": url, "data": data, "headers": headers,
                              "allow_redirects": allow_redirects})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _stored():
    return FakeResponse(200, {"status": 200, "success": True,
                              "message": "SigCare intake stored successfully"})


def _client(*responses, **overrides):
    config = IntakeConfig(url=URL, jwt_secret=SECRET, max_attempts=3, **overrides)
    session = FakeSession(*responses)
    return IntakeClient(config, session=session, sleep=lambda _: None), session


def test_sends_result_unchanged_with_fresh_signed_token():
    client, session = _client(_stored())
    outcome = client.send(RESULT)

    assert (outcome.status, outcome.attempts, outcome.api_status) == (DELIVERED, 1, 200)
    (request,) = session.requests
    assert request["url"] == URL
    assert json.loads(request["data"]) == RESULT
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["allow_redirects"] is False

    claims = jwt.decode(request["headers"]["ACCESS-TOKEN"], SECRET, algorithms=["HS256"])
    assert claims["timestamp"].startswith("SigCare-") and len(claims["timestamp"]) == 18
    assert len(claims["code"]) == 36
    assert time.time() < claims["exp"] <= time.time() + 300


def test_duplicate_job_id_counts_as_delivered():
    client, _ = _client(FakeResponse(200, {"status": 802, "success": False,
                                          "message": "already exists"}))
    outcome = client.send(RESULT)
    assert outcome.status == DUPLICATE and outcome.ok


def test_server_and_token_errors_are_retried_with_a_new_token_each_time():
    client, session = _client(
        FakeResponse(200, {"status": 500, "success": False, "message": "boom"}),
        FakeResponse(200, {"status": 501, "success": False, "message": "Invalid Request"}),
        _stored(),
    )
    outcome = client.send(RESULT)
    assert (outcome.status, outcome.attempts) == (DELIVERED, 3)
    tokens = [r["headers"]["ACCESS-TOKEN"] for r in session.requests]
    codes = [jwt.decode(t, SECRET, algorithms=["HS256"])["code"] for t in tokens]
    assert len(set(codes)) == 3  # single-use tokens are never reused


def test_network_errors_are_retried_then_reported():
    client, session = _client(*(requests.ConnectionError("down") for _ in range(3)))
    outcome = client.send(RESULT)
    assert (outcome.status, outcome.attempts, outcome.ok) == (FAILED, 3, False)
    assert "ConnectionError" in outcome.message


def test_missing_parameters_are_not_retried():
    client, session = _client(FakeResponse(200, {"status": 801, "success": False,
                                                "message": "results is required"}))
    outcome = client.send(RESULT)
    assert (outcome.status, outcome.attempts, outcome.api_status) == (FAILED, 1, 801)


def test_redirects_are_not_followed():
    client, _ = _client(FakeResponse(302, None, {"Location": "https://elsewhere.example/"}))
    outcome = client.send(RESULT)
    assert (outcome.status, outcome.attempts) == (FAILED, 1)


def test_result_without_job_id_is_never_sent():
    client, session = _client(_stored())
    assert client.send({"results": []}).status == FAILED
    assert session.requests == []


def test_disabled_client_sends_nothing():
    config = IntakeConfig(url="", jwt_secret="", enabled=False)
    session = FakeSession()
    assert IntakeClient(config, session=session).send(RESULT).status == DISABLED
    assert session.requests == []


@pytest.mark.parametrize("url, secret", [
    ("http://intake.example.test/api", SECRET),      # not HTTPS
    ("intake.example.test/api", SECRET),             # not a URL
    (URL, ""),                                       # no secret
    (URL, "short"),                                  # weak secret
])
def test_unsafe_configuration_is_rejected_at_start(url, secret):
    with pytest.raises(ValueError):
        IntakeClient(IntakeConfig(url=url, jwt_secret=secret))


def test_secret_is_not_in_repr():
    assert SECRET not in repr(IntakeConfig(url=URL, jwt_secret=SECRET))


# ------------------------- S3 worker integration -------------------------

@pytest.fixture
def fake_textract(monkeypatch):
    monkeypatch.setattr(processing, "extract_prescription_pdf",
                        lambda **kwargs: {"flag_for_review": False, "review_reason": []})


def _watcher(tmp_path, *responses):
    client, session = _client(*responses)
    watcher = S3Watcher(WatcherConfig(bucket="test-bucket", work_dir=tmp_path / "work"),
                        s3=FakeS3(), intake=client)
    watcher.s3.objects[f"{DAY}/input/Doc 1.pdf"] = _pdf_bytes()
    return watcher, session


def test_worker_sends_the_saved_result_and_records_delivery(tmp_path, fake_textract):
    watcher, session = _watcher(tmp_path, _stored())
    assert watcher.process_key(f"{DAY}/input/Doc 1.pdf").startswith("COMPLETED")

    saved = json.loads(watcher.s3.objects[f"{DAY}/processed/Doc 1.json"])
    assert json.loads(session.requests[0]["data"]) == saved   # the same JSON, as is
    delivery = json.loads(watcher.s3.objects[f"{INTER}/manifest/delivery.json"])
    assert delivery["status"] == DELIVERED and delivery["job_id"] == saved["job_id"]
    status = json.loads(watcher.s3.objects[f"{INTER}/manifest/status.json"])
    assert status["intake_delivery"] == DELIVERED


def test_failed_delivery_keeps_the_processed_result(tmp_path, fake_textract):
    watcher, _ = _watcher(tmp_path, *(requests.Timeout("slow") for _ in range(3)))
    assert watcher.process_key(f"{DAY}/input/Doc 1.pdf").startswith("COMPLETED")

    assert f"{DAY}/processed/Doc 1.json" in watcher.s3.objects
    assert f"{DAY}/input/Doc 1.pdf" not in watcher.s3.objects
    delivery = json.loads(watcher.s3.objects[f"{INTER}/manifest/delivery.json"])
    assert delivery["status"] == FAILED and delivery["attempts"] == 3
