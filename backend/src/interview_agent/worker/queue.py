"""At-least-once Streams transport; acknowledge only durable SQL outcomes or invalid messages."""
from interview_agent.storage.errors import ApiError

import asyncio
import re

from pydantic import ValidationError
from redis.exceptions import ResponseError

from interview_agent.contracts.operations import QueueMessage
from interview_agent import observability
from .assessment import AssessmentUnavailable


def parse_message(fields):
    if set(fields) != {"job_id", "operation_id", "resource_version"}:
        return None
    version = fields["resource_version"]
    if not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", version):
        return None
    try:
        return QueueMessage.model_validate(dict(fields, resource_version=int(version)))
    except (ValidationError, TypeError):
        return None


class QueueTransport:
    group = "workers-v1"

    def __init__(self, redis, stream: str):
        if not re.fullmatch(r"py-interview:(?:development|test)(?::[a-f0-9]{32})?:jobs:v1(?::career|:research|:materials|:interviews|:preparations|:written|:coding)?", stream):
            raise ValueError("python_stream_required")
        self.redis, self.stream = redis, stream
        self.cursor = "0-0"

    async def ensure_group(self):
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
        except ResponseError as error:
            if not str(error).startswith("BUSYGROUP"):
                raise

    async def publish(self, message: QueueMessage):
        return await self.redis.xadd(self.stream, {k: str(v) for k, v in message.model_dump().items()})

    async def receive(self, consumer: str, idle_ms=1000):
        await self.ensure_group()
        claimed = await self.redis.xautoclaim(self.stream, self.group, consumer, idle_ms, self.cursor, count=1)
        self.cursor = claimed[0]
        if claimed[1]:
            return claimed[1][0]
        rows = await self.redis.xreadgroup(self.group, consumer, {self.stream: ">"}, count=1)
        return rows[0][1][0] if rows else None

    async def ack(self, message_id):
        await self.redis.xack(self.stream, self.group, message_id)


class OutboxDispatcher:
    def __init__(self, repository, queue):
        self.repository, self.queue = repository, queue

    async def dispatch(self, recovery_seconds=5):
        count = 0
        for row in await self.repository.pending_dispatch(recovery_seconds):
            message = QueueMessage(job_id=str(row["job_id"]), operation_id=str(row["operation_id"]), resource_version=row["resource_version"])
            await self.queue.publish(message)
            # Crash in this gap permits duplicate publication by design.
            await self.repository.mark_dispatched(row["outbox_id"])
            count += 1
        return count


class TaskWorker:
    def __init__(self, repository, queue, consumer, *, lease_seconds=30, reclaim_idle_ms=1000, prepare=None, assess=None, claimed_assess=None, tracer=None):
        self.repository, self.queue, self.consumer = repository, queue, consumer
        self.lease_seconds, self.reclaim_idle_ms, self.prepare = lease_seconds, reclaim_idle_ms, prepare
        self.tracer = tracer if tracer is not None else observability.current()
        self.assess = assess
        self.claimed_assess = claimed_assess
        if assess is not None and claimed_assess is not None:
            raise ValueError("one_assessment_executor_required")

    async def once(self):
        notification = await self.queue.receive(self.consumer, self.reclaim_idle_ms)
        if notification is None:
            return False
        message_id, fields = notification
        message = parse_message(fields)
        if message is None:
            # Malformed notification cannot identify a task; discard only this message.
            await self.queue.ack(message_id)
            self.tracer.record('queue.rejected', stage='queue', reason_code='malformed_message', error_category='queue')
            return True
        self.tracer.record('queue.notification', stage='queue', job_id=message.job_id,
                           operation_id=message.operation_id)
        claim = await self.repository.claim(message, self.lease_seconds)
        if claim is None:
            # The repository already recorded the precise reason when it knows one.
            self.tracer.record('worker.claim_rejected', stage='worker', reason_code='claim_not_granted',
                               error_category='queue', job_id=message.job_id, operation_id=message.operation_id)
            if await self.repository.can_ack(message):
                await self.queue.ack(message_id)
            return True
        with observability.correlation(session_id=getattr(claim, 'session_id', None),
                                       interview_run_id=claim.message.operation_id,
                                       operation_id=claim.message.operation_id,
                                       job_id=claim.message.job_id, attempt=claim.attempt):
            return await self._process(message_id, message, claim)

    async def _process(self, message_id, message, claim):
        lost = asyncio.Event()
        stop = asyncio.Event()
        # One trace entry per claimed attempt; provider/tool spans nest under it.
        attempt = self.tracer.open('worker.attempt', stage='worker', fencing_token=claim.fencing_token)

        async def renew():
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.lease_seconds / 3)
                    return
                except TimeoutError:
                    pass
                try:
                    if not await self.repository.heartbeat(claim, self.lease_seconds):
                        self.tracer.record('worker.lease_lost', stage='worker', reason_code='heartbeat_rejected',
                                           error_category='queue', attempt=claim.attempt)
                        lost.set()
                        return
                except Exception:
                    self.tracer.record('worker.lease_lost', stage='worker', reason_code='heartbeat_failed',
                                       error_category='queue', attempt=claim.attempt)
                    lost.set()
                    return

        renewal = asyncio.create_task(renew())
        persisted_failure = False
        try:
            ready = True
            if claim.kind == "assess_answer" and await self.repository.saved_assessment(claim) is None:
                if self.assess is None and self.claimed_assess is None:
                    raise AssessmentUnavailable("model_not_enabled")
                context = await self.repository.assessment_context(claim)
                # Paid HTTP admission also needs this exact attempt/fencing token.
                output = (await self.claimed_assess(claim, context, lost) if self.claimed_assess is not None
                          else await self.assess(context, lost))
                ready = not lost.is_set() and await self.repository.save_assessment(claim, output)
            if ready and self.prepare:
                await self.prepare(claim, lost)
            if ready and not lost.is_set():
                committed = await self.repository.complete(claim)
                if committed:
                    attempt.ok(outcome='committed')
                    self.tracer.record('worker.result_committed', stage='worker')
        except AssessmentUnavailable as error:
            attempt.fail(error.code)
            persisted_failure = await self.repository.fail_assessment(claim, waiting=True, code=error.code)
        except Exception:
            attempt.fail('worker_attempt_error')
            persisted_failure = await self.repository.fail_attempt(claim)
        finally:
            stop.set()
            await renewal
            attempt.end()
        try:
            ackable = persisted_failure or await self.repository.can_ack(message)
        except ApiError:
            # A stale message whose goal/session was removed (a test cleanup, a deleted goal)
            # must not kill the worker: every restart would replay the same poison message.
            # Ack it, record why, and keep consuming.
            self.tracer.record('worker.stale_message', stage='queue', reason_code='resource_unavailable',
                               error_category='queue', attempt=claim.attempt)
            ackable = True
        if ackable:
            await self.queue.ack(message_id)
            self.tracer.record('worker.acked', stage='queue')
        return True
