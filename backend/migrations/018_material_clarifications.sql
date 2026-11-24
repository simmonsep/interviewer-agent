ALTER TABLE app.career_material_sources ADD COLUMN clarification_context jsonb;
CREATE UNIQUE INDEX career_material_clarification_once ON app.career_material_sources
 (goal_id,(clarification_context->>'run_id')) WHERE clarification_context IS NOT NULL;
