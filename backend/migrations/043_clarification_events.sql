-- F07Z-167: a clarification is its own event. It restates scope for the current question, never
-- changes the question, never occupies a round and never produces an observation.
ALTER TABLE app.career_interview_events
  DROP CONSTRAINT IF EXISTS career_interview_events_kind_check;
ALTER TABLE app.career_interview_events
  ADD CONSTRAINT career_interview_events_kind_check
  CHECK(kind IN ('question','answer','explanation','skip','clarification'));
