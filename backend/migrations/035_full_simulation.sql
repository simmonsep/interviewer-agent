-- F07Z-167: full simulation (45 minutes, up to 12 asked rounds).
-- Quick practice keeps its 1-6 cap; the preparation row now also records which profile was
-- frozen so the session sees the same one after a retry. No rewrite of existing rows: the
-- column is nullable and old rows keep their values.
ALTER TABLE app.career_interview_preparations
  DROP CONSTRAINT IF EXISTS career_interview_preparations_max_questions_check;
ALTER TABLE app.career_interview_preparations
  ADD CONSTRAINT career_interview_preparations_max_questions_check
  CHECK(max_questions BETWEEN 1 AND 12);
ALTER TABLE app.career_interview_preparations
  ADD COLUMN IF NOT EXISTS profile text
  CHECK(profile IN ('quick','full'));
