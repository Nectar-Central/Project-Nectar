-- v251: ranked rooms admin allowlist
-- Backs the /ranked/rooms admin surface (server/routes/ranked_rooms.py).
-- Gate is fail closed: a request only passes when its discord_id is a row
-- here. Insert/remove your operators directly in Supabase:
--   insert into ranked_admins (discord_id, added_by) values ('<your did>', 'founder');
--
-- RLS: the server uses the service key (bypasses RLS). If you expose this
-- table to client keys, enable RLS and deny by default:
--   alter table ranked_admins enable row level security;

create table if not exists ranked_admins (
  discord_id text primary key,
  added_by   text,
  created_at timestamptz not null default now()
);

-- v251.1 (optional, run only if you expose client keys to this table):
-- alter table ranked_admins enable row level security;
