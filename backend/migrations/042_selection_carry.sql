-- F07Z-166 D1: when a training turn keeps the same JD/conditions and the selected position is
-- still current, the server materialises an equivalent selection for the new goal version. The
-- provenance is stored so the plan card can say "carried from version N" without guessing.
ALTER TABLE app.career_job_selections
 ADD COLUMN carried_from_version integer;
