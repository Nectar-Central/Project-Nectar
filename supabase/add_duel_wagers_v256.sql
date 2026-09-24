-- v2.56 duel wagers (idempotent). Apply in Supabase SQL editor.
-- PvP Nectar-Coin duels: targeted (@opponent) + open (first-join-locks 1v1).
-- Balances stay on bngc_users.nectar_coins; escrow/refund/payout all flow
-- through bngc_coin_ledger via the bot (reasons duel_escrow/duel_refund/duel_win).
-- Match proof is read from bngc_game_events (room_id + match_version + gm +
-- short_code/code); this table only tracks wager state + sealed versions.

create table if not exists public.bngc_duel_wagers (
  id uuid primary key default gen_random_uuid(),
  guild_id text not null,
  kind text not null default 'targeted', -- targeted | open
  status text not null default 'open',   -- open | pending | active | resolved | cancelled | expired
  channel_id text not null default '',
  message_id text not null default '',
  thread_id text not null default '',
  creator_id text not null,
  opponent_id text,                      -- targeted: the challenged user; open: NULL until locked
  stake integer not null default 0,
  pot integer not null default 0,
  mode text not null default 'zw_lg',    -- zw_lg | bf | df (bf covers bf/bf2v2/BF2V2)
  first_to integer not null default 1,
  race text not null default 'wins',     -- wins (zw_lg/bf) | kills (df)
  score_creator integer not null default 0,
  score_opponent integer not null default 0,
  kills_creator integer not null default 0,
  kills_opponent integer not null default 0,
  visibility text not null default 'public', -- public | private
  room_code text,                        -- optional pre-bound 5-char pID
  room_id text,                          -- bound private room_id once identified
  sealed jsonb not null default '[]'::jsonb, -- [{room_id, match_version, winner, kills_c, kills_o, gm}]
  expires_at timestamptz,
  active_since timestamptz,
  resolved_at timestamptz,
  winner_id text,
  created_at timestamptz not null default now()
);

create index if not exists bngc_duel_wagers_status_idx
  on public.bngc_duel_wagers(status, expires_at);
create index if not exists bngc_duel_wagers_players_idx
  on public.bngc_duel_wagers(creator_id, opponent_id, status);
create index if not exists bngc_duel_wagers_thread_idx
  on public.bngc_duel_wagers(thread_id);

alter table public.bngc_duel_wagers enable row level security;
-- The bot uses the service-role key. It bypasses RLS.

-- Rollback (run only to revert v2.53):
-- drop table if exists public.bngc_duel_wagers;
