-- F07Z-167 (2026-11-21 decision): a decision round may now use five provider requests, so one
-- rejected submission can be repaired without exhausting the round. Raise the per-run check to
-- 5 and the per-session fuse to 80 (13 tasks x 5 plus margin). The authorization fuse and the
-- user's money cap still bound actual calls and spend.
ALTER TABLE app.career_interviews
  DROP CONSTRAINT IF EXISTS career_interviews_model_requests_check;
ALTER TABLE app.career_interviews
  ADD CONSTRAINT career_interviews_model_requests_check
  CHECK(model_requests BETWEEN 0 AND 80);
ALTER TABLE app.career_interview_runs
  DROP CONSTRAINT IF EXISTS career_interview_runs_model_requests_check;
ALTER TABLE app.career_interview_runs
  ADD CONSTRAINT career_interview_runs_model_requests_check
  CHECK(model_requests BETWEEN 0 AND 5);
