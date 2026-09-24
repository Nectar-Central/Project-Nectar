-- Wager voids (v261). The creator can void a match from the duel thread
-- (opponent crashed / false start). Voiding awards nobody the round: the seal
-- is removed from bngc_duel_wagers.sealed and the (room_id, match_version) is
-- remembered in `voided` so it can never seal later. Running totals are
-- recomputed from the remaining seals.

alter table public.bngc_duel_wagers
  add column if not exists voided jsonb not null default '[]'::jsonb;
-- [{room_id, match_version, by, reason, at}]

-- Rollback (run only to revert v261):
-- alter table public.bngc_duel_wagers drop column if exists voided;
