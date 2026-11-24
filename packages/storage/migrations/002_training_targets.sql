CREATE SCHEMA app;
CREATE TABLE app.training_targets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id text NOT NULL REFERENCES auth."user"(id) ON DELETE CASCADE,
  request_id text NOT NULL CHECK (length(request_id) BETWEEN 8 AND 128),
  request_hash text NOT NULL CHECK (length(request_hash)=64),
  role_package_id text NOT NULL CHECK (role_package_id='agent-intern'),
  role_package_version text NOT NULL,
  level text NOT NULL CHECK (level='intern'),
  focus text NOT NULL DEFAULT '' CHECK (length(focus)<=120),
  context_mode text NOT NULL DEFAULT 'role_only' CHECK (context_mode='role_only'),
  version integer NOT NULL DEFAULT 1 CHECK (version>=1),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id,request_id)
);
CREATE INDEX training_targets_owner_created_idx ON app.training_targets(user_id,created_at,id);
