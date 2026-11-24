ALTER TABLE app.career_steps ADD COLUMN cycle integer NOT NULL DEFAULT 0 CHECK(cycle BETWEEN 0 AND 3);
ALTER TABLE app.career_steps DROP CONSTRAINT career_steps_goal_id_goal_version_key;
ALTER TABLE app.career_steps ADD CONSTRAINT career_steps_version_cycle_key UNIQUE(goal_id,goal_version,cycle);
CREATE OR REPLACE FUNCTION app.career_input_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.goal_id,NEW.goal_version,NEW.cycle,NEW.request_id,NEW.request_hash,NEW.message,NEW.input_payload)
  IS DISTINCT FROM (OLD.goal_id,OLD.goal_version,OLD.cycle,OLD.request_id,OLD.request_hash,OLD.message,OLD.input_payload) THEN
  RAISE EXCEPTION 'career_input_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
