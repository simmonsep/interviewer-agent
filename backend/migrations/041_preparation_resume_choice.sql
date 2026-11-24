-- F07Z-166: an explicit "this session uses no resume" must survive both repository layers.
-- Omitting draft_id alone still means "use the selected position's bound resume", so the choice
-- needs its own column instead of being inferred from a null draft.
ALTER TABLE app.career_interview_preparations
 ADD COLUMN use_resume boolean NOT NULL DEFAULT true;
