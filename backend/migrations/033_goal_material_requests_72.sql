-- F07Z-120 (decision THIRD_OPTION): the goal-level material fuse was 16, sized in 031 for one
-- analysis plus one rewrite. The desktop now reuses one authorization across runs, so a
-- second long rewrite in the same goal was refused at 9 + 9 = 18 even after the
-- authorization-level counter is removed. Materials reuse the existing 72-call non-coding
-- allocation (`career/budget.py`), which `legacy_budget_exhausted` already enforces; this
-- migration only aligns the per-goal material column with that capacity. Spending is still
-- bounded by the authorization cap, and the per-run column stays at 16 (one run, 12 requests).
ALTER TABLE app.career_goals DROP CONSTRAINT career_goals_material_model_requests_check;
ALTER TABLE app.career_goals ADD CONSTRAINT career_goals_material_model_requests_check
    CHECK (material_model_requests BETWEEN 0 AND 72);
