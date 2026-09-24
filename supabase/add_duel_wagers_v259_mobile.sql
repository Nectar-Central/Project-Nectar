-- Duel wagers: mobile-duel flag (v259). A mobile player (no companion, no
-- captures) may join/accept when the creator is live+tracked; the duel is
-- then scored by name against the tracked side's room events.

alter table public.bngc_duel_wagers
  add column if not exists is_mobile_duel boolean not null default false;

-- Rollback (run only to revert v259):
-- alter table public.bngc_duel_wagers drop column if exists is_mobile_duel;
