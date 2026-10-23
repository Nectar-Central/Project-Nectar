-- Multi-guild Nectar (v263) — one bot, many Discord servers.
-- Apply in the Supabase SQL editor. Idempotent: safe to run twice.
--
-- What this adds:
--   1. nectar_guilds         — one row per Discord server using Nectar
--   2. nectar_guild_admins   — who can manage that server (owner/admin/moderator)
--   3. nectar_guild_players  — per-server player profile + per-server PR
--   4. guild_id on comp tables so a server only ever sees its own tourneys
--   5. backfill: every existing row belongs to the original Nectar server
--
-- Nothing here changes how capture works. A capture is still one session ->
-- one engine -> one processor. Only the *tourney* tables gain an owner.

-- ---------------------------------------------------------------------------
-- 1. Guild registry
-- ---------------------------------------------------------------------------

create table if not exists public.nectar_guilds (
  id bigint primary key,                       -- Discord guild (server) ID
  name text not null,
  icon_url text,
  owner_id text not null,                      -- Discord user ID of the server owner
  is_active boolean not null default true,
  settings jsonb not null default '{
    "bot_nickname": "Nectar",
    "accent_color": "#7c3aed",
    "results_channel_id": null,
    "announce_channel_id": null,
    "leaderboard_public": true,
    "default_points_table": {"kill": 1, "win": 3, "placement": [10, 7, 5, 3, 1]}
  }'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- 2. Guild staff
-- ---------------------------------------------------------------------------

create table if not exists public.nectar_guild_admins (
  guild_id bigint not null references public.nectar_guilds(id) on delete cascade,
  user_id text not null,                       -- Discord user ID
  role text not null default 'admin',
  added_by text,
  added_at timestamptz not null default now(),
  primary key (guild_id, user_id),
  constraint nectar_guild_admins_role check (role in ('owner', 'admin', 'moderator'))
);

create index if not exists nectar_guild_admins_user_idx
  on public.nectar_guild_admins(user_id);

-- ---------------------------------------------------------------------------
-- 3. Per-guild player profile
-- ---------------------------------------------------------------------------
-- Stats are deliberately NOT shared between servers: a kill in server A must
-- never move a leaderboard in server B. bngc_users stays the global login
-- identity (token, game_username, lock state); this table is the per-server
-- scoreboard row.

create table if not exists public.nectar_guild_players (
  guild_id bigint not null references public.nectar_guilds(id) on delete cascade,
  discord_id text not null,                    -- -> bngc_users.discord_id
  game_username text,                          -- last seen in-game name in this server
  pr numeric not null default 1000,
  kills integer not null default 0,
  wins integer not null default 0,
  matches integer not null default 0,
  points integer not null default 0,
  is_banned boolean not null default false,
  ban_reason text,
  notes text,
  registered_at timestamptz not null default now(),
  last_played_at timestamptz,
  primary key (guild_id, discord_id)
);

create index if not exists nectar_guild_players_discord_idx
  on public.nectar_guild_players(discord_id);
create index if not exists nectar_guild_players_board_idx
  on public.nectar_guild_players(guild_id, points desc);

-- ---------------------------------------------------------------------------
-- 3b. One row per (tournament, player) actually banked
-- ---------------------------------------------------------------------------
-- This is the receipt for a closed tournament. The primary key makes banking
-- idempotent: a retry can only insert rows that do not exist yet, so totals
-- can never be counted twice. It also answers "where did these points come
-- from?" without replaying every match.

create table if not exists public.nectar_guild_stat_awards (
  guild_id bigint not null references public.nectar_guilds(id) on delete cascade,
  tourney_id integer not null references public.bngc_comp_tourneys(id) on delete cascade,
  discord_id text not null,
  kills integer not null default 0,
  wins integer not null default 0,
  matches integer not null default 0,
  points integer not null default 0,
  pr numeric,
  applied_at timestamptz not null default now(),
  primary key (tourney_id, discord_id)
);

create index if not exists nectar_guild_stat_awards_guild_idx
  on public.nectar_guild_stat_awards(guild_id, applied_at desc);

-- ---------------------------------------------------------------------------
-- 4. Own the comp tables
-- ---------------------------------------------------------------------------

alter table public.bngc_comp_tourneys
  add column if not exists guild_id bigint references public.nectar_guilds(id);

alter table public.bngc_comp_pr_history
  add column if not exists guild_id bigint references public.nectar_guilds(id);

alter table public.bngc_room_tags
  add column if not exists guild_id bigint references public.nectar_guilds(id);

-- Dashboard-only tourney fields. The bot command already had most of these in
-- a long slash-command signature; the dashboard needs them as plain columns
-- so a form can edit them later.
alter table public.bngc_comp_tourneys
  add column if not exists description text,
  add column if not exists pr_enabled boolean not null default true,
  add column if not exists registration_open boolean not null default true,
  add column if not exists updated_at timestamptz not null default now(),
  -- Set the moment a closed tourney's results have been folded into
  -- nectar_guild_players. Guards against double-counting on a second close.
  add column if not exists stats_applied_at timestamptz;

-- ---------------------------------------------------------------------------
-- 5. Backfill to the original Nectar server
-- ---------------------------------------------------------------------------
-- 1512788791805349888 is the existing Nectar Central guild. Every tourney that
-- predates this migration was run there, so claiming it for that guild keeps
-- every historical board and PR row visible exactly as before.

insert into public.nectar_guilds (id, name, owner_id)
values (1512788791805349888, 'Nectar Central', '0')
on conflict (id) do nothing;

update public.bngc_comp_tourneys
   set guild_id = 1512788791805349888
 where guild_id is null;

update public.bngc_comp_pr_history
   set guild_id = 1512788791805349888
 where guild_id is null;

update public.bngc_room_tags
   set guild_id = 1512788791805349888
 where guild_id is null;

create index if not exists bngc_comp_tourneys_guild_idx
  on public.bngc_comp_tourneys(guild_id, status);
create index if not exists bngc_comp_pr_history_guild_idx
  on public.bngc_comp_pr_history(guild_id, discord_id);

-- ---------------------------------------------------------------------------
-- 6. Dashboard login sessions
-- ---------------------------------------------------------------------------
-- Discord OAuth hands the browser an opaque token; the server maps it to a
-- Discord user + the guilds they can manage. Kept in Postgres (not a cookie
-- JWT) so revoking a session is a delete and works across serverless workers.

create table if not exists public.nectar_sessions (
  token text primary key,
  discord_id text not null,
  discord_username text,
  avatar_url text,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  last_seen_at timestamptz not null default now()
);

create index if not exists nectar_sessions_expiry_idx
  on public.nectar_sessions(expires_at);

-- ---------------------------------------------------------------------------
-- Rollback (reverse FK order; run only to revert):
-- ---------------------------------------------------------------------------
-- drop table if exists public.nectar_sessions;
-- drop index if exists public.nectar_guild_stat_awards_guild_idx;
-- drop table if exists public.nectar_guild_stat_awards;
-- drop index if exists public.bngc_comp_pr_history_guild_idx;
-- drop index if exists public.bngc_comp_tourneys_guild_idx;
-- alter table public.bngc_room_tags drop column if exists guild_id;
-- alter table public.bngc_comp_pr_history drop column if exists guild_id;
-- alter table public.bngc_comp_tourneys
--   drop column if exists guild_id,
--   drop column if exists description,
--   drop column if exists pr_enabled,
--   drop column if exists registration_open,
--   drop column if exists updated_at;
-- drop table if exists public.nectar_guild_players;
-- drop table if exists public.nectar_guild_admins;
-- drop table if exists public.nectar_guilds;
