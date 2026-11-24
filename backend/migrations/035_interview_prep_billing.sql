-- S03b: planning calls get their own billing purpose so the interview caps stay separate.
-- The list must repeat every existing purpose, or the previous ones stop being insertable.
ALTER TABLE app.model_http_calls DROP CONSTRAINT model_http_calls_purpose_check;
ALTER TABLE app.model_http_calls ADD CHECK(purpose IN ('assessment','career_coordination','career_research',
  'career_materials','career_interview','career_coding','career_interview_prep'));
