"""Bounded, unauthenticated reads of two observed employer detail endpoints.

These are website endpoints, not guaranteed public developer APIs. A readable
description does not attest an open vacancy or an applicant's eligibility.
"""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

import httpx
from pydantic import Field, field_validator, model_validator

from interview_agent.contracts.common import Contract
from .content import visible_text

SourceId = Literal["xiaomi-intern", "tme-campus"]
ProbeStatus = Literal["detail_readable", "access_denied", "rate_limited", "not_found",
                      "http_error", "redirect_refused", "invalid_payload", "response_too_large",
                      "timeout", "network_error"]
MAX_BYTES = 1_000_000
MAX_TEXT = 30_000
USER_AGENT = "PythonInterviewSourceProbe/0.1 (read-only feasibility check)"


class JobDetail(Contract):
    job_id: str
    company: str
    title: Annotated[str, Field(min_length=1, max_length=500)]
    cities: list[str]
    recruitment_label: str | None
    responsibilities: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]
    requirements: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]
    published_raw: str | int | None
    availability: Literal["unknown"] = "unknown"


class SourceProbe(Contract):
    schema_version: Literal["source-probe-v1"] = "source-probe-v1"
    source_id: SourceId
    job_id: str
    source_url: str
    retrieval_url: str
    observed_at: datetime
    status: ProbeStatus
    http_status: Annotated[int, Field(ge=100, le=599)] | None = None
    response_sha256: str | None = None
    content_sha256: str | None = None
    job: JobDetail | None = None

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_required")
        return value

    @model_validator(mode="after")
    def result_shape(self):
        if self.status == "detail_readable":
            if self.job is None or self.job.job_id != self.job_id or not self.response_sha256 or not self.content_sha256:
                raise ValueError("readable_detail_required")
        elif self.job is not None or self.content_sha256 is not None:
            raise ValueError("failed_probe_has_no_job")
        return self


def endpoints(source_id: SourceId, job_id: str) -> tuple[str, str]:
    # IDs stay strings. No caller-supplied URL, host, query or redirects.
    if not isinstance(job_id, str):
        raise ValueError("invalid_job_id")
    if source_id == "xiaomi-intern" and re.fullmatch(r"[0-9]{16,24}", job_id):
        host = "https://xiaomi.jobs.f.mioffice.cn"
        return (f"{host}/internship/position/{job_id}/detail",
                f"{host}/api/v1/job/posts/{job_id}?portal_type=6&with_recommend=false")
    if source_id == "tme-campus" and re.fullmatch(r"[0-9]{1,10}", job_id):
        host = "https://join.tencentmusic.com"
        return f"{host}/campus/post-details/?id={job_id}", f"{host}/api/uc-job/info?id={job_id}"
    raise ValueError("unsupported_source_or_invalid_job_id")


def text(value) -> str:
    if not isinstance(value, str):
        raise ValueError("text_required")
    result = visible_text(value)
    if not result or len(result) > MAX_TEXT:
        raise ValueError("invalid_text")
    return result


def _label(value) -> str | None:
    return text(value) if isinstance(value, str) and value.strip() else None


def parse_detail(source_id: SourceId, job_id: str, payload: object) -> JobDetail:
    endpoints(source_id, job_id)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("invalid_payload")
    if source_id == "xiaomi-intern":
        if type(payload.get("code")) is not int or payload["code"] != 0:
            raise ValueError("upstream_error")
        raw = payload["data"]["job_post_detail"]
        locations = raw.get("city_list")
        if locations is None:
            locations = [raw["city_info"]] if raw.get("city_info") else []
        cities = [text(city["name"]) for city in locations]
        recruitment = raw.get("recruit_type")
        label = _label(recruitment.get("name")) if isinstance(recruitment, dict) else None
        values = dict(company="小米", title=text(raw["title"]), cities=cities, recruitment_label=label,
                      responsibilities=text(raw["description"]), requirements=text(raw["requirement"]),
                      published_raw=raw.get("publish_time"))
    else:
        if payload.get("code") != "200":
            raise ValueError("upstream_error")
        raw = payload["data"]
        # obj_city is the interview location; it must never become a work city.
        cities = [text(city["label"]) for city in raw.get("work_city", [])]
        values = dict(company="腾讯音乐", title=text(raw["name"]), cities=cities,
                      recruitment_label=_label(raw.get("job_type_descr")),
                      responsibilities=text(raw["duty"]), requirements=text(raw["requirement"]),
                      published_raw=raw.get("date"))
    if raw.get("id") != job_id:
        raise ValueError("job_identity_mismatch")
    values["cities"] = list(dict.fromkeys(values["cities"]))
    return JobDetail(job_id=job_id, **values)


async def probe(source_id: SourceId, job_id: str, client: httpx.AsyncClient) -> SourceProbe:
    page_url, api_url = endpoints(source_id, job_id)
    base = dict(source_id=source_id, job_id=job_id, source_url=page_url,
                retrieval_url=api_url, observed_at=datetime.now(timezone.utc))
    metadata = {}
    try:
        async with asyncio.timeout(15):
            async with client.stream("GET", api_url, follow_redirects=False,
                                     headers={"User-Agent": USER_AGENT}) as response:
                status = response.status_code
                metadata["http_status"] = status
                errors = {401: "access_denied", 403: "access_denied", 404: "not_found", 429: "rate_limited"}
                if status != 200:
                    reason = errors.get(status, "redirect_refused" if 300 <= status < 400 else "http_error")
                    return SourceProbe(**base, **metadata, status=reason)
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > MAX_BYTES:
                        return SourceProbe(**base, **metadata, status="response_too_large")
                    body.extend(chunk)
                metadata["response_sha256"] = hashlib.sha256(body).hexdigest()
                if "json" not in response.headers.get("content-type", "").lower():
                    return SourceProbe(**base, **metadata, status="invalid_payload")
                try:
                    job = parse_detail(source_id, job_id, json.loads(body))
                except (ValueError, KeyError, TypeError, AttributeError):
                    return SourceProbe(**base, **metadata, status="invalid_payload")
                canonical = json.dumps(job.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                return SourceProbe(**base, **metadata, status="detail_readable", job=job,
                                   content_sha256=hashlib.sha256(canonical.encode()).hexdigest())
    except (TimeoutError, httpx.TimeoutException):
        return SourceProbe(**base, **metadata, status="timeout")
    except httpx.HTTPError:
        return SourceProbe(**base, **metadata, status="network_error")
