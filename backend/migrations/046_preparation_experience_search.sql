ALTER TABLE app.career_interview_preparations
    ADD COLUMN IF NOT EXISTS experience_search_admitted_at timestamptz;
