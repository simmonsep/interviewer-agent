CREATE TABLE app.career_applications (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 request_id text NOT NULL, request_hash text NOT NULL, job_key text NOT NULL,
 status text NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','SUBMITTED','INTERVIEW','OFFER','REJECTED','WITHDRAWN','UNKNOWN')),
 version integer NOT NULL DEFAULT 1, package_revision integer NOT NULL DEFAULT 1,
 submitted_package_revision integer, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id,request_id), UNIQUE(goal_id,job_key)
);
CREATE TABLE app.career_application_packages (
 application_id uuid NOT NULL REFERENCES app.career_applications(id) ON DELETE CASCADE,
 revision integer NOT NULL CHECK(revision BETWEEN 1 AND 30), payload jsonb NOT NULL, sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(application_id,revision)
);
CREATE TABLE app.career_application_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), application_id uuid NOT NULL REFERENCES app.career_applications(id) ON DELETE CASCADE,
 seq integer NOT NULL, package_revision integer NOT NULL, request_id text NOT NULL, request_hash text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('package','progress','availability')), payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(application_id,seq), UNIQUE(application_id,request_id),
 FOREIGN KEY(application_id,package_revision) REFERENCES app.career_application_packages(application_id,revision)
);
CREATE TRIGGER career_application_package_immutable BEFORE UPDATE ON app.career_application_packages FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER career_application_event_immutable BEFORE UPDATE ON app.career_application_events FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,UPDATE,DELETE ON app.career_applications TO py_interview_app;
GRANT SELECT,INSERT,DELETE ON app.career_application_packages,app.career_application_events TO py_interview_app;
