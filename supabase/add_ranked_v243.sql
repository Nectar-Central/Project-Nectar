-- Ranked seal-time scoring table (v243).
-- points_table: the lobby-strength static table {kill, win, placement}
-- used for one seal, so boards/recalc reproduce the seal-time economics.
-- Seals tolerate its absence (classic row shape fallback).
ALTER TABLE public.ranked_results
  ADD COLUMN IF NOT EXISTS points_table jsonb;
