-- Ranked match event bank (v240): comp-style kill/alv/leave rows per ranked
-- seal, keyed by (room_id, match_version). Lets ranked kill/placement/win
-- detection read evidence instead of snapshots. Banked best-effort at seal
-- time; seals never fail when this table is missing/unreachable.
CREATE TABLE IF NOT EXISTS public.bngc_ranked_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  room_id text NOT NULL,
  match_version integer NOT NULL,
  etype text NOT NULL,
  actor_nr integer,
  actor_name text,
  target_name text,
  discord_id text,
  alive boolean,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT bngc_ranked_events_etype CHECK (etype IN ('kill', 'knock', 'alv', 'leave'))
);
CREATE INDEX IF NOT EXISTS bngc_ranked_events_room_ver_idx
  ON public.bngc_ranked_events(room_id, match_version);
