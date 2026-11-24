-- F07Z-59: let a practice record carry the problem statement the user pasted.
-- The product still never reads the site page, so without this column the interview agent
-- would only know the title and the user's own note. The statement is optional: leaving it
-- empty keeps the record useful for "what did I do" and honest about "I have no statement".
ALTER TABLE app.career_practice_records ADD COLUMN statement text NOT NULL DEFAULT '';
