-- Ranked tourneys v249: per-match frozen dynamic scoring.
-- Dynamic tourney scoring (upset kills, scaled win/placement, tiered loss)
-- is computed from ranked_points at seal time and frozen here, so boards
-- and recalculations replay identical numbers forever. Matches sealed
-- before this column existed fall back to the static tourney table with
-- no loss (legacy behavior preserved). Idempotent.
alter table public.bngc_ranked_tourney_matches
  add column if not exists points_table jsonb;
alter table public.bngc_ranked_tourney_matches
  add column if not exists avg_points numeric;

-- Rollback (run only to revert):
-- alter table public.bngc_ranked_tourney_matches drop column if exists avg_points;
-- alter table public.bngc_ranked_tourney_matches drop column if exists points_table;
