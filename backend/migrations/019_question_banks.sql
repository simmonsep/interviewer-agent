CREATE TABLE app.question_bank_imports (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 request_id text NOT NULL, request_hash text NOT NULL, document jsonb NOT NULL, content_sha256 text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(user_id,request_id), UNIQUE(id,user_id)
);
CREATE TABLE app.question_banks (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 current_version integer NOT NULL CHECK(current_version BETWEEN 1 AND 30), enabled boolean NOT NULL DEFAULT true,
 revision integer NOT NULL DEFAULT 1 CHECK(revision>=1), UNIQUE(id,user_id)
);
CREATE TABLE app.question_bank_versions (
 bank_id uuid NOT NULL, user_id uuid NOT NULL, version integer NOT NULL CHECK(version BETWEEN 1 AND 30),
 preview_id uuid NOT NULL UNIQUE, document jsonb NOT NULL, content_sha256 text NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(bank_id,version), UNIQUE(user_id,request_id),
 FOREIGN KEY(bank_id,user_id) REFERENCES app.question_banks(id,user_id) ON DELETE CASCADE,
 FOREIGN KEY(preview_id,user_id) REFERENCES app.question_bank_imports(id,user_id)
);
CREATE TRIGGER question_bank_import_immutable BEFORE UPDATE ON app.question_bank_imports
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER question_bank_version_immutable BEFORE UPDATE ON app.question_bank_versions
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,DELETE ON app.question_bank_imports,app.question_bank_versions TO py_interview_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON app.question_banks TO py_interview_app;
