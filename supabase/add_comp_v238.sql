-- Comp [PR] nickname opt-in (v238).
-- Users opting in via /tourney-name get their server nickname set to
-- "Display Name [PR]" after each tourney PR calculation.
ALTER TABLE bngc_users
  ADD COLUMN IF NOT EXISTS tourney_name_optin boolean NOT NULL DEFAULT false;
