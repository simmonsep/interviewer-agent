-- Amounts are integer nano-CNY (1 CNY = 1,000,000,000), not binary floating point.
CREATE TABLE app.model_authorizations (
 id text PRIMARY KEY, policy_hash text NOT NULL CHECK(length(policy_hash)=64),
 max_nano bigint NOT NULL CHECK(max_nano BETWEEN 1 AND 2000000000),
 state text NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','PAUSED')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.model_http_calls (
 id uuid PRIMARY KEY, authorization_id text NOT NULL REFERENCES app.model_authorizations(id),
 -- Accounting survives deletion of practice data. These copied IDs deliberately have no cascading FK.
 operation_id uuid NOT NULL, job_id uuid NOT NULL, attempt integer NOT NULL,
 fencing_token bigint NOT NULL, input_hash text NOT NULL CHECK(length(input_hash)=64),
 payload_hash text NOT NULL CHECK(length(payload_hash)=64), payload_bytes integer NOT NULL CHECK(payload_bytes>0),
 reserved_nano bigint NOT NULL CHECK(reserved_nano>0),
 state text NOT NULL DEFAULT 'RESERVED' CHECK(state IN ('RESERVED','KNOWN','UNKNOWN')),
 accounted_nano bigint, raw_usage jsonb, http_status integer, response_model text, error_code text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), settled_at timestamptz,
 CHECK((state='KNOWN' AND accounted_nano>=0 AND accounted_nano<=reserved_nano AND raw_usage IS NOT NULL)
    OR (state IN ('RESERVED','UNKNOWN') AND accounted_nano IS NULL))
);
CREATE INDEX model_calls_authorization_idx ON app.model_http_calls(authorization_id,created_at,id);
CREATE INDEX model_calls_operation_idx ON app.model_http_calls(operation_id,created_at,id);
GRANT SELECT ON app.model_authorizations TO py_interview_app;
GRANT UPDATE(state) ON app.model_authorizations TO py_interview_app;
GRANT SELECT,INSERT,UPDATE ON app.model_http_calls TO py_interview_app;
