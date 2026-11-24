"""F07Z-167: the full-simulation session profile.

The quick practice keeps its own numbers (the caller's `max_questions`/`duration_seconds`).
`profile='full'` asks the server for the 45-minute, 10-12 round simulation: the server owns the
numbers so a stale tab cannot shrink the session, and the frozen policy records them.

The clock starts when the first question is published, not when the row is created: preparation
and the first decision run inside `SETUP_GRACE_SECONDS`, then the deadline is rewritten once.
"""

FULL = 'full'
FULL_MAX_QUESTIONS = 12
FULL_DURATION_SECONDS = 2700
FULL_TARGET_MINIMUM = 10
FULL_SETUP_GRACE_SECONDS = 300
# Inside the last three minutes the interviewer may finish below the target instead of opening a
# new complex question; the design document calls this closing time.
FULL_CLOSING_SECONDS = 180


def resolved(profile, max_questions, duration_seconds):
    """Server-side numbers for this request. Non-full requests keep the caller's values."""
    if profile == FULL:
        return FULL_MAX_QUESTIONS, FULL_DURATION_SECONDS
    return max_questions, duration_seconds


def minimum_questions(profile, capacity):
    """How many asked rounds a normal finish must have.

    Full simulation wants ten, but a small topic pack must still be able to finish: the server
    never demands more rounds than it has valid candidates for.
    """
    if profile != FULL:
        return None
    return min(FULL_TARGET_MINIMUM, max(1, capacity))


def capacity(topic_order, topics, facts, project_allowed):
    """How many distinct rounds the frozen candidates can actually support.

    A role/bank topic contributes its authored base and probe questions (retest only exists in
    coaching). Each usable resume fact can carry the four project focus dimensions. The count is
    deliberately conservative: it is the floor a full session may be required to reach, not a
    target the interviewer must fill with invented questions.
    """
    rounds = 0
    for identity in topic_order:
        item = topics.get(identity) or {}
        rounds += sum(1 for kind in ('base', 'probe') if item.get(kind))
    if project_allowed:
        rounds += len(facts) * 4
    return rounds


def is_closing(policy, remaining_seconds):
    return (policy.get('profile') == FULL and remaining_seconds is not None
            and remaining_seconds <= FULL_CLOSING_SECONDS)
