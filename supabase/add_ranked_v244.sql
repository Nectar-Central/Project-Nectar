-- Ranked presence heartbeat (v244): who is in which room RIGHT NOW.
-- The userscript pings this while tracking a code (it knows the room; the
-- server otherwise only sees game EVENTs, and a quiet lobby writes none).
-- /api/ranked/verify treats a fresh ping as host presence alongside
-- killfeed/did evidence. Keyed by (code, discord_id) — codes recycle over
-- time but the freshness window (120s) makes collisions harmless. Stale
-- rows simply age out (no cleanup job needed for correctness).
CREATE TABLE IF NOT EXISTS public.bngc_ranked_presence (
  code text NOT NULL,
  discord_id text NOT NULL,
  seen_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT bngc_ranked_presence_pkey PRIMARY KEY (code, discord_id)
);
CREATE INDEX IF NOT EXISTS bngc_ranked_presence_seen_idx
  ON public.bngc_ranked_presence(seen_at desc);
