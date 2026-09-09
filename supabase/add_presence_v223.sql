-- Room tags for presence (idempotent). Apply in Supabase SQL editor.
-- Hosts tag a private room code so friends see "In Scrims - <mode>" or
-- "In Tourney - <name>" instead of a bare "In Match". One row per code;
-- re-tagging overwrites. No packet signal distinguishes scrims/tourneys,
-- so the name always comes from a human (bot /scrim, /tourney, /untag).

create table if not exists public.bngc_room_tags (
  code text primary key, -- 5-char pID, uppercased
  kind text not null,    -- 'scrim' | 'tourney'
  name text not null,    -- display name (tourney name, or scrim label)
  created_by text,        -- discord_id of the tagger (accountability)
  created_at timestamptz not null default now(),
  constraint bngc_room_tags_kind check (kind in ('scrim', 'tourney'))
);

-- Rollback (run only to revert):
-- drop table if exists public.bngc_room_tags;
