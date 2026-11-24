-- F07Z-40: a batched rewrite needs up to 8 model requests per run (4 batches + 2 reads
-- + self-corrections). The old cap of 6 was a fuse, not a contract; the policy layer keeps
-- its own `max_http_attempts` and the authorization keeps the monetary cap, so widening
-- this counter does not widen spending beyond what the policy already allows.
ALTER TABLE app.career_material_runs DROP CONSTRAINT career_material_runs_model_requests_check;
ALTER TABLE app.career_material_runs ADD CONSTRAINT career_material_runs_model_requests_check
    CHECK (model_requests BETWEEN 0 AND 8);
