-- Ranked rooms: match_version column (v241).
-- server/lib/ranked.py selects match_version on every room lookup; without
-- it every lookup fails with Postgres 42703 and ranked silently never
-- activates (no stats block, no UI, no seals). roster/updated_at are added
-- IF NOT EXISTS as well since the same lookup reads them.
ALTER TABLE public.ranked_rooms
  ADD COLUMN IF NOT EXISTS match_version integer NOT NULL DEFAULT 0;
ALTER TABLE public.ranked_rooms
  ADD COLUMN IF NOT EXISTS roster jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.ranked_rooms
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
