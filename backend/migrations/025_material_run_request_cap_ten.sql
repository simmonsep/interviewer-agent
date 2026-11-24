-- F07Z-44: the batched rewrite path measures 3 reads + 3 batches before corrections, so the
-- per-run request counter needs room for four corrections. The money guard stays where it was:
-- every provider request still reserves against the paid authorization before it is sent.
ALTER TABLE app.career_material_runs DROP CONSTRAINT career_material_runs_model_requests_check;
ALTER TABLE app.career_material_runs ADD CONSTRAINT career_material_runs_model_requests_check
    CHECK (model_requests BETWEEN 0 AND 10);
