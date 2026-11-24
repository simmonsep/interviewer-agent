"""S06B: bounded, deterministic progress summary for one interview round.

The loop must not treat "same tool name" or "same error code" as no progress: a repeated read of
the same topic is different from a re-read after a new topic, and a corrected submission is
different from a re-sent one. This module only *summarises* what the trace already records; it
does not decide anything and it never stores model or user text (submissions are hashed).
"""
from __future__ import annotations

import hashlib
import json


def submission_hash(arguments) -> str:
    """A stable short fingerprint of one submission, never the content itself."""
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]


def summarize(trace) -> dict:
    """Count the signals a "no progress" rule would need, with denominators."""
    rounds = sorted({entry['round'] for entry in trace if isinstance(entry.get('round'), int)})
    read_topic_rounds = [entry for entry in trace
                         if entry.get('tool') == 'read_topics' and entry.get('status') == 'ok']
    new_evidence_rounds = sorted({entry['round'] for entry in read_topic_rounds
                                  if entry.get('new_topics', 0) > 0})
    repeats = []
    previous = None
    for entry in trace:
        if entry.get('tool') != 'submit_interview_action':
            continue
        fingerprint = entry.get('submission_hash')
        if entry.get('status') == 'error' and fingerprint and fingerprint == previous:
            repeats.append(entry['round'])
        previous = fingerprint
    same_reads = 0
    last_reads = None
    for entry in read_topic_rounds:
        topics = tuple(entry.get('topics') or ())
        if topics and topics == last_reads:
            same_reads += 1
        last_reads = topics or last_reads
    trailing = 0
    for entry in reversed(read_topic_rounds):
        if entry.get('new_topics', 0) > 0:
            break
        trailing += 1
    return {'rounds': len(rounds),
            'model_rounds': max(rounds) if rounds else 0,
            'read_rounds': len(read_topic_rounds),
            'new_evidence_rounds': len(new_evidence_rounds),
            'identical_rejected_submissions': len(repeats),
            'repeated_read_rounds': same_reads,
            'trailing_read_repeats': trailing,
            'candidate_produced': any(entry.get('tool') == 'submit_interview_action' and
                                      entry.get('status') == 'ok' for entry in trace)}


def would_stop_early(progress: dict) -> bool:
    """The conservative rule S06B is allowed to test.

    Stop only when the *trailing* rounds show no new evidence: either two final reads that added
    nothing, or two consecutive identical rejections. The legal fourth-round correction in the
    frozen bank sample shows neither pattern, so the rule must not touch it. Not wired into
    production: the measurement below shows it cannot save a call inside the four-round ceiling.
    """
    if progress.get('candidate_produced'):
        return False
    if progress.get('trailing_read_repeats', 0) >= 2:
        return True
    return progress.get('identical_rejected_submissions', 0) >= 2
