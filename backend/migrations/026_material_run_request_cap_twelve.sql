-- F07Z-46: the per-run request ceiling is now derived from the batch count (2 reads +
-- batches + 4 corrections, hard cap 12), so the stored counter needs the same ceiling.
ALTER TABLE app.career_material_runs DROP CONSTRAINT career_material_runs_model_requests_check;
ALTER TABLE app.career_material_runs ADD CONSTRAINT career_material_runs_model_requests_check
    CHECK (model_requests BETWEEN 0 AND 12);
