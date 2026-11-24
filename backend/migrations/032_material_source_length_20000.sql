-- F07Z-117: detailed experience is normal. The 4000-character boundary was a product limit,
-- not a safety one; 20,000 keeps a DoS ceiling while accepting 6-10 pages of detail.
ALTER TABLE app.career_material_sources DROP CONSTRAINT career_material_sources_body_check;
ALTER TABLE app.career_material_sources ADD CONSTRAINT career_material_sources_body_check
    CHECK (length(body) BETWEEN 1 AND 20000);
ALTER TABLE app.career_material_file_previews DROP CONSTRAINT career_material_file_previews_body_check;
ALTER TABLE app.career_material_file_previews ADD CONSTRAINT career_material_file_previews_body_check
    CHECK (length(body) BETWEEN 1 AND 20000);
