-- F07Z-167: a skipped question gets its own event. It keeps the round count and the request
-- identity, but it never carries an observation, so the review cannot mark it as wrong.
ALTER TABLE app.career_interview_events
  DROP CONSTRAINT IF EXISTS career_interview_events_kind_check;
ALTER TABLE app.career_interview_events
  ADD CONSTRAINT career_interview_events_kind_check
  CHECK(kind IN ('question','answer','explanation','skip'));
