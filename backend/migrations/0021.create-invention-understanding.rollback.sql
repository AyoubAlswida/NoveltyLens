DROP TABLE IF EXISTS understanding_regeneration_requests;
DROP TABLE IF EXISTS approved_understandings;
DROP TABLE IF EXISTS understanding_proposals;
ALTER TABLE verification_runs DROP COLUMN IF EXISTS inventor_novelty_statement;
ALTER TABLE verification_runs DROP COLUMN IF EXISTS problem_solved;
