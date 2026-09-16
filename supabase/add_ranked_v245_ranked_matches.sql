-- Ranked v245: one durable match entity per room/version.
-- bngc_ranked_events remains the append-only evidence feed and ranked_results
-- remains the legacy award ledger.  This table is additive, so rolling the
-- migration out does not interrupt either existing writer.

ALTER TABLE public.ranked_rooms
  ADD COLUMN IF NOT EXISTS max_matches integer;

CREATE TABLE IF NOT EXISTS public.bngc_ranked_matches (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  room_id text NOT NULL REFERENCES public.ranked_rooms(id) ON DELETE CASCADE,
  match_version integer NOT NULL,
  room_code text,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'sealed', 'backed_out')),
  winner_discord_id text,
  placements jsonb NOT NULL DEFAULT '{}'::jsonb,
  point_awards jsonb NOT NULL DEFAULT '{}'::jsonb,
  removed_discord_ids text[] NOT NULL DEFAULT ARRAY[]::text[],
  points_table jsonb,
  started_at timestamptz,
  sealed_at timestamptz,
  UNIQUE (room_id, match_version)
);

CREATE INDEX IF NOT EXISTS bngc_ranked_matches_room_status_idx
  ON public.bngc_ranked_matches(room_id, status, match_version DESC);

-- Backfill every legacy seal.  The upsert keeps this safe to run more than
-- once, and only fills the entity fields that legacy results actually know.
INSERT INTO public.bngc_ranked_matches (
  room_id, match_version, room_code, status, winner_discord_id, placements,
  point_awards, points_table, sealed_at
)
SELECT
  rr.room_id,
  rr.match_version,
  rooms.short_code,
  'sealed',
  rr.winner_discord_id,
  COALESCE(rr.placements, '{}'::jsonb),
  -- Legacy result points are final scored totals, not manual adjustments.
  -- Do not copy them into point_awards or a later Recalc would double-pay.
  '{}'::jsonb,
  rr.points_table,
  now()
FROM public.ranked_results rr
LEFT JOIN public.ranked_rooms rooms ON rooms.id = rr.room_id
ON CONFLICT (room_id, match_version) DO UPDATE SET
  room_code = COALESCE(EXCLUDED.room_code, bngc_ranked_matches.room_code),
  status = 'sealed',
  winner_discord_id = EXCLUDED.winner_discord_id,
  placements = EXCLUDED.placements,
  point_awards = bngc_ranked_matches.point_awards,
  points_table = COALESCE(EXCLUDED.points_table, bngc_ranked_matches.points_table),
  sealed_at = COALESCE(bngc_ranked_matches.sealed_at, EXCLUDED.sealed_at);

-- A stable dual-read surface for new dashboards/jobs during the transition.
-- Keep reading ranked_results directly in old clients; do not drop it.
CREATE OR REPLACE VIEW public.bngc_ranked_results_v245 AS
SELECT
  m.room_id, m.match_version, m.room_code, m.status, m.winner_discord_id,
  m.placements, m.point_awards AS points, m.removed_discord_ids,
  m.points_table, m.started_at, m.sealed_at
FROM public.bngc_ranked_matches m
UNION ALL
SELECT
  r.room_id, r.match_version, rooms.short_code, 'sealed'::text,
  r.winner_discord_id, COALESCE(r.placements, '{}'::jsonb),
  '{}'::jsonb, ARRAY[]::text[], r.points_table,
  NULL::timestamptz, NULL::timestamptz
FROM public.ranked_results r
LEFT JOIN public.ranked_rooms rooms ON rooms.id = r.room_id
WHERE NOT EXISTS (
  SELECT 1 FROM public.bngc_ranked_matches m
  WHERE m.room_id = r.room_id AND m.match_version = r.match_version
);
