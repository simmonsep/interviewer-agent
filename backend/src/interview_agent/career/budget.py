"""Preserve the original role allocation alongside 48 coding calls."""

# F07Z-167 (decision A, 2026-11-21): 72 could not cover two full simulations (about 39 calls
# each), so the non-coding pool now matches the goal's 120-call total. Materials, research and
# interviews share it; each capability still has its own per-run and authorization fuses.
NON_CODING_REQUEST_ALLOCATION = 120


def legacy_budget_exhausted(goal):
    return goal['model_requests'] >= 120 or goal['model_requests'] - goal.get('coding_model_requests', 0) >= NON_CODING_REQUEST_ALLOCATION
