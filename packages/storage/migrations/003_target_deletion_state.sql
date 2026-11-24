-- Append a migration rather than rewriting the already applied 002 checksum.
ALTER TABLE app.training_targets ADD COLUMN deleted_at timestamptz;
