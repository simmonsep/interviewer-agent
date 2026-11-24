"""Candidate deterministic guard for material rewrites — now a thin delegate.

The rules moved to `interview_agent.materials.claim_guard` (S00G) because the product rewrite
chain needs the same implementation the C2 evaluation measured. This module stays as the
evaluation entry point so older tests and reports keep their import path; there is exactly one
rule set.
"""
from interview_agent.materials.claim_guard import (ALL_FAMILIES, CODE, REVIEWED_FAMILIES,  # noqa: F401
                                                   codes, flagged_as_violation, scan)
