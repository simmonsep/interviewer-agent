ALTER TABLE app.career_goals DROP CONSTRAINT career_goals_model_requests_check;
ALTER TABLE app.career_goals ADD CHECK(model_requests BETWEEN 0 AND 120);
ALTER TABLE app.career_goals ADD COLUMN coding_model_requests integer NOT NULL DEFAULT 0 CHECK(coding_model_requests BETWEEN 0 AND 48);
ALTER TABLE app.career_coding_sessions ADD COLUMN model_requests integer NOT NULL DEFAULT 0 CHECK(model_requests BETWEEN 0 AND 24);
ALTER TABLE app.career_coding_runs ADD COLUMN model_requests integer NOT NULL DEFAULT 0 CHECK(model_requests BETWEEN 0 AND 12);
ALTER TABLE app.career_coding_runs ADD COLUMN origin text CHECK(origin IN ('scripted_protocol','real_model'));
UPDATE app.career_coding_runs SET origin='scripted_protocol' WHERE kind='assist' AND assistant_responses>0;
ALTER TABLE app.model_http_calls DROP CONSTRAINT model_http_calls_purpose_check;
ALTER TABLE app.model_http_calls ADD CHECK(purpose IN ('assessment','career_coordination','career_research','career_materials','career_interview','career_coding'));
CREATE FUNCTION app.coding_origin_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.origin IS NOT NULL AND NEW.origin IS DISTINCT FROM OLD.origin THEN
  RAISE EXCEPTION 'coding_origin_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER coding_origin_immutable BEFORE UPDATE ON app.career_coding_runs FOR EACH ROW EXECUTE FUNCTION app.coding_origin_immutable();
