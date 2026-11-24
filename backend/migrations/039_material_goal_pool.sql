-- F07Z-167 (decision A): the non-coding pool is now 120 calls per goal so two full simulations
-- (about 78 calls) fit. The goal-level material column was aligned to the old 72; align it with
-- the pool so materials can use the same headroom. Per-run and authorization fuses are unchanged.
ALTER TABLE app.career_goals
  DROP CONSTRAINT IF EXISTS career_goals_material_model_requests_check;
ALTER TABLE app.career_goals
  ADD CONSTRAINT career_goals_material_model_requests_check
  CHECK(material_model_requests BETWEEN 0 AND 120);
