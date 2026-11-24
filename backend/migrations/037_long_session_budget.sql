-- F07Z-167 (decision A): a 10-12 round full simulation needs 39-52 model requests; the session
-- counter was capped at 32. Raise the fuse to 64. The authorization HTTP fuse and the user's
-- money cap still bound actual calls and spend.
ALTER TABLE app.career_interviews
  DROP CONSTRAINT IF EXISTS career_interviews_model_requests_check;
ALTER TABLE app.career_interviews
  ADD CONSTRAINT career_interviews_model_requests_check
  CHECK(model_requests BETWEEN 0 AND 64);
