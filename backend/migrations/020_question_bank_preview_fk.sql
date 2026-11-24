-- Account deletion cascades through both imports and banks; check their composite
-- reference after both paths complete, while still rejecting standalone preview deletion.
ALTER TABLE app.question_bank_versions
 ALTER CONSTRAINT question_bank_versions_preview_id_user_id_fkey DEFERRABLE INITIALLY DEFERRED;
