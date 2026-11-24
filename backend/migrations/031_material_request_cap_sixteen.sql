-- F07Z-99: the per-goal material fuse was 12, which cannot hold analysis (up to 6) plus a
-- batched rewrite (up to 8) on a 41-line resume; the second run was refused after the first
-- succeeded. Raise the counter to 16. Spending is still bounded by the authorization (2 CNY)
-- and the per-run ceiling; this is capacity, not a new budget.
ALTER TABLE app.career_goals DROP CONSTRAINT career_goals_material_model_requests_check;
ALTER TABLE app.career_goals ADD CONSTRAINT career_goals_material_model_requests_check
    CHECK (material_model_requests BETWEEN 0 AND 16);
ALTER TABLE app.career_material_runs DROP CONSTRAINT career_material_runs_model_requests_check;
ALTER TABLE app.career_material_runs ADD CONSTRAINT career_material_runs_model_requests_check
    CHECK (model_requests BETWEEN 0 AND 16);
