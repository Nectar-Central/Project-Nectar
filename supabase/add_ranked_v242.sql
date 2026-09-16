-- Ranked nickname opt-in (v242).
-- ranked_name_optin: members opted into the Display Name [Milestone]
-- server nickname (mirrors tourney_name_optin for [PR]).
ALTER TABLE public.bngc_users
  ADD COLUMN IF NOT EXISTS ranked_name_optin boolean NOT NULL DEFAULT false;
-- Dashboard linking lock (v242): locked rows refuse game-name changes
-- (dashboard link, /change-username, /comp-change-user, /ranked register
-- rename) until unlocked. Hosts lock identities that are verified correct.
ALTER TABLE public.bngc_users
  ADD COLUMN IF NOT EXISTS ranked_link_locked boolean NOT NULL DEFAULT false;
