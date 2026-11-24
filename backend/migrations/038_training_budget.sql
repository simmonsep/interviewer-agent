-- F07Z-167 (decision A): the per-goal training counter was capped at 48, enough for one
-- full simulation (about 39 calls) but not two. Raise the database fuse to 80; the non-coding
-- allocation and the user's money cap still bound actual work.
ALTER TABLE app.career_goals
  DROP CONSTRAINT IF EXISTS career_goals_training_model_requests_check;
ALTER TABLE app.career_goals
  ADD CONSTRAINT career_goals_training_model_requests_check
  CHECK(training_model_requests BETWEEN 0 AND 80);
