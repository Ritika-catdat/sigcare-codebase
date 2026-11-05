"""Deliver finished results to the Healthya SigCare Intake API.

    POST <SIGCARE_INTAKE_URL>            e.g. https://staging-api.healthya.co.uk/api/sigcare/intakes
    ACCESS-TOKEN: <JWT, HS256>           new single-use token for every request (and every retry)
    Content-Type: application/json
    body: the result JSON exactly as saved to processed/<file>.json

JWT claims (Healthya integration guide, section 2.3):
    timestamp  "SigCare-YYYY-MM-DD" (current calendar date)
    code       a fresh UUID per request (one-time nonce)
    exp        Unix expiry; 10 minutes or less is recommended

Responses carry their own code in the body "status" field, which may differ from
the HTTP status:
    200  stored                         -> DELIVERED
    802  job_id already stored          -> DUPLICATE (already delivered; not an error)
    801  required parameters missing    -> FAILED, not retried (the payload is wrong)
    501  missing/invalid/reused token   -> retried with a new token
    500  server error                   -> retried

Security: the shared secret comes only from the environment, is never logged,
and is hidden from repr(). Only HTTPS endpoints are accepted, redirects are
not followed (so patient data cannot be sent elsewhere), and the payload is
never logged.

Resend a stored result by hand (e.g. after a FAILED delivery):
    python -m sigcare_quality.intake_client s3://<bucket>/<yyyy>/<MMM>/<dd>/processed/<file>.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import jwt
import requests
from requests.adapters import HTTPAdapter

from .observability import configure_logging, emit_metric, emit_metrics, log_context

LOG = logging.getLogger("sigcare_quality.intake_client")

# Body "status" codes from the Healthya integration guide (see module docstring).
API_DUPLICATE = 802
API_INVALID_TOKEN = 501
API_SERVER_ERROR = 500

DELIVERED = "DELIVERED"
DUPLICATE = "DUPLICATE"
FAILED = "FAILED"
DISABLED = "DISABLED"

# Response messages are logged; keep them short in case the server echoes input.
_MAX_MESSAGE_CHARS = 300


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


@dataclass(frozen=True)
class IntakeConfig:
    url: str
    jwt_secret: str = field(repr=False)
    enabled: bool = True
    token_header: str = "ACCESS-TOKEN"
    token_ttl_seconds: int = 300
    token_timezone: str = "Europe/London"
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0
    max_attempts: int = 5
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "IntakeConfig":
        return cls(
            enabled=_env_bool("SIGCARE_INTAKE_ENABLED"),
            url=os.getenv("SIGCARE_INTAKE_URL", "").strip(),
            jwt_secret=os.getenv("SIGCARE_INTAKE_JWT_SECRET", "").strip(),
            token_header=os.getenv("SIGCARE_INTAKE_TOKEN_HEADER", "ACCESS-TOKEN").strip(),
            token_ttl_seconds=int(os.getenv("SIGCARE_INTAKE_TOKEN_TTL_SECONDS", "300")),
            token_timezone=os.getenv("SIGCARE_INTAKE_TOKEN_TIMEZONE", "Europe/London").strip(),
            read_timeout_seconds=float(os.getenv("SIGCARE_INTAKE_TIMEOUT_SECONDS", "30")),
            max_attempts=int(os.getenv("SIGCARE_INTAKE_MAX_ATTEMPTS", "5")),
        )

    def validate(self) -> None:
        """Fail fast at startup on a configuration that could never deliver safely."""
        if not self.enabled:
            return
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("SIGCARE_INTAKE_URL must be a full https:// URL.")
        if len(self.jwt_secret) < 32:
            raise ValueError("SIGCARE_INTAKE_JWT_SECRET is missing or too short.")
        if not 30 <= self.token_ttl_seconds <= 600:
            raise ValueError("SIGCARE_INTAKE_TOKEN_TTL_SECONDS must be between 30 and 600.")
        if self.max_attempts < 1:
            raise ValueError("SIGCARE_INTAKE_MAX_ATTEMPTS must be at least 1.")
        ZoneInfo(self.token_timezone)

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc


@dataclass
class DeliveryResult:
    status: str                       # DELIVERED | DUPLICATE | FAILED | DISABLED
    job_id: str | None
    attempts: int = 0
    http_status: int | None = None
    api_status: int | None = None
    message: str | None = None
    duration_s: float = 0.0
    endpoint: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (DELIVERED, DUPLICATE, DISABLED)

    def to_record(self) -> dict[str, Any]:
        return {**asdict(self), "recorded_at": datetime.now(ZoneInfo("UTC")).isoformat()}


class IntakeClient:
    """Thread-safe client; reuse one instance so connections are pooled."""

    def __init__(self, config: IntakeConfig, session: requests.Session | None = None,
                 sleep=time.sleep) -> None:
        config.validate()
        self.config = config
        self._sleep = sleep
        self._session = session or self._new_session()

    @classmethod
    def from_env(cls) -> "IntakeClient":
        return cls(IntakeConfig.from_env())

    @staticmethod
    def _new_session() -> requests.Session:
        session = requests.Session()
        # Retries are handled below (each one needs a new token), not by urllib3.
        adapter = HTTPAdapter(pool_connections=2, pool_maxsize=8, max_retries=0)
        session.mount("https://", adapter)
        return session

    def make_token(self) -> str:
        now = datetime.now(ZoneInfo(self.config.token_timezone))
        claims = {
            "timestamp": f"SigCare-{now.date().isoformat()}",
            "code": str(uuid.uuid4()),
            "exp": int(time.time()) + self.config.token_ttl_seconds,
        }
        return jwt.encode(claims, self.config.jwt_secret, algorithm="HS256")

    def send(self, result: dict[str, Any]) -> DeliveryResult:
        """POST one result. Never raises: the outcome is returned and logged."""
        job_id = result.get("job_id")
        outcome = DeliveryResult(status=FAILED, job_id=job_id, endpoint=self.config.host)
        if not self.config.enabled:
            outcome.status = DISABLED
            return outcome
        if not job_id or not isinstance(result.get("results"), list):
            # The API rejects these (801); do not send patient data we know will bounce.
            outcome.message = "Result has no job_id or results; not sent."
            LOG.error("Intake delivery skipped: %s", outcome.message,
                      extra={"intake_status": FAILED})
            emit_metric("IntakeFailed")
            return outcome

        body = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        started = time.monotonic()
        with log_context(job_id=job_id):
            LOG.info("Sending result to intake API %s (%d bytes)", self.config.host, len(body),
                     extra={"intake_status": "SENDING", "payload_bytes": len(body)})
            for attempt in range(1, self.config.max_attempts + 1):
                outcome.attempts = attempt
                retry = self._attempt(body, outcome)
                if not retry or attempt == self.config.max_attempts:
                    break
                delay = min(self.config.backoff_max_seconds,
                            self.config.backoff_base_seconds * 2 ** (attempt - 1))
                delay *= random.uniform(0.8, 1.2)
                LOG.warning("Intake attempt %d/%d failed (%s); retrying in %.1fs",
                            attempt, self.config.max_attempts, outcome.message, delay,
                            extra={"intake_status": "RETRYING", "attempt": attempt,
                                   "http_status": outcome.http_status,
                                   "api_status": outcome.api_status})
                self._sleep(delay)

            outcome.duration_s = round(time.monotonic() - started, 2)
            fields = {"intake_status": outcome.status, "attempts": outcome.attempts,
                      "http_status": outcome.http_status, "api_status": outcome.api_status,
                      "duration_s": outcome.duration_s}
            if outcome.status == DELIVERED:
                LOG.info("Intake API stored the result", extra=fields)
            elif outcome.status == DUPLICATE:
                LOG.info("Intake API already had this job_id; nothing to do", extra=fields)
            else:
                LOG.error("Intake delivery FAILED after %d attempt(s): %s",
                          outcome.attempts, outcome.message, extra=fields)
        emit_metrics({"IntakeDelivered": int(outcome.status == DELIVERED),
                      "IntakeDuplicate": int(outcome.status == DUPLICATE),
                      "IntakeFailed": int(outcome.status == FAILED)})
        emit_metric("IntakeSeconds", outcome.duration_s, unit="Seconds")
        return outcome

    def _attempt(self, body: bytes, outcome: DeliveryResult) -> bool:
        """One POST with a fresh token. Updates outcome; returns True if worth retrying."""
        headers = {
            self.config.token_header: self.make_token(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = self._session.post(
                self.config.url, data=body, headers=headers,
                timeout=(self.config.connect_timeout_seconds, self.config.read_timeout_seconds),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            outcome.http_status = outcome.api_status = None
            outcome.message = f"{type(exc).__name__}: {str(exc)[:_MAX_MESSAGE_CHARS]}"
            return True

        outcome.http_status = response.status_code
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        api_status = data.get("status")
        outcome.api_status = api_status if isinstance(api_status, int) else None
        message = data.get("message") or response.reason or ""
        outcome.message = str(message)[:_MAX_MESSAGE_CHARS]
        code = outcome.api_status or response.status_code

        if code == API_DUPLICATE:
            outcome.status = DUPLICATE
            return False
        if 200 <= response.status_code < 300 and data.get("success") is True:
            outcome.status = DELIVERED
            return False
        outcome.status = FAILED
        if 300 <= response.status_code < 400:
            outcome.message = "Unexpected redirect; not followed."
            return False
        # Token and server problems may clear up; anything else (801 missing
        # parameters, other 4xx, an unexpected body) will fail the same way again.
        return code in (API_INVALID_TOKEN, API_SERVER_ERROR, 429) or response.status_code >= 500


def main() -> None:
    parser = argparse.ArgumentParser(description="Resend stored result JSON files to the intake API.")
    parser.add_argument("locations", nargs="+",
                        help="s3://bucket/key.json of a processed result, or a local .json path")
    args = parser.parse_args()
    configure_logging("sigcare-intake")
    config = IntakeConfig.from_env()
    if not config.enabled:
        sys.exit("SIGCARE_INTAKE_ENABLED is not true; nothing sent.")
    client = IntakeClient(config)

    s3 = None
    failed = 0
    for location in args.locations:
        if location.startswith("s3://"):
            import boto3

            s3 = s3 or boto3.client("s3", region_name=os.getenv("AWS_REGION", "eu-west-2"))
            bucket, _, key = location[5:].partition("/")
            result = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        else:
            with open(location, encoding="utf-8") as handle:
                result = json.load(handle)
        with log_context(source_key=location):
            outcome = client.send(result)
        print(f"{location}: {outcome.status} (api_status={outcome.api_status}, "
              f"attempts={outcome.attempts})")
        failed += not outcome.ok
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
