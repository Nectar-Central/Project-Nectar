create table if not exists public.players (
  id text primary key,
  username text not null,
  display_name text not null,
  game_handle text not null,
  avatar_url text,
  registered_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists players_registered_at_idx on public.players (registered_at);

alter table public.players enable row level security;

-- The bot and API should use a Supabase service-role key.
