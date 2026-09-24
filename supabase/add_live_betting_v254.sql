-- v2.54 live-room betting (idempotent). Apply in Supabase SQL editor.
-- Extends the v253 bet kind with 'live' (any public/private room,
-- keyed live:{room_id}:{match_version}, results read from the
-- bngc_game_events evidence rows the capture pipeline already banks).
-- Requires add_match_betting_v253.sql first.

alter table public.bngc_match_bets drop constraint if exists bngc_match_bets_kind;
alter table public.bngc_match_bets
  add constraint bngc_match_bets_kind check (kind in ('comp', 'rt', 'room', 'live'));

-- Rollback (run only to revert v2.54):
-- alter table public.bngc_match_bets drop constraint if exists bngc_match_bets_kind;
-- alter table public.bngc_match_bets
--   add constraint bngc_match_bets_kind check (kind in ('comp', 'rt', 'room'));
