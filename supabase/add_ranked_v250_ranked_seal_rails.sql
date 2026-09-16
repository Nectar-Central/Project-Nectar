-- Ranked seal rails (v250): database-enforced idempotency for the ranked
-- dual-recorder design. The host tracker (companion/comp-intake.js) and the
-- server engine bridge can both seal a (room_id, match_version); whichever
-- lands first must win and every retry must be a no-op. Until now that was
-- only an app-level convention; these constraints make it a database fact.
--
-- Conventions followed: idempotent (safe to run more than once), additive
-- (rolling the migration out interrupts no existing writer), legacy rows
-- keep working unchanged. Rollback statements live in comments at the bottom.
--
-- NOTE on data safety: statement 2 rewrites rows (marks superseded open
-- matches as 'backed_out'); statements 1 and 3-7 are pure DDL and cannot
-- lose data. Read-only admin queries to preview what statement 2 would
-- touch are included at the bottom — run them first if you want a look.

-- ---------------------------------------------------------------------------
-- 1) ranked_results: one row per (room_id, match_version), explicitly.
-- seal_match already upserts with on_conflict="room_id,match_version" and its
-- duplicate-check reads this table, so the live DB almost certainly has the
-- constraint (it predates the supabase/*.sql file set). IF NOT EXISTS makes
-- this a no-op there and a guard for fresh environments rebuilt from the repo.
CREATE UNIQUE INDEX IF NOT EXISTS ranked_results_room_ver_uidx
  ON public.ranked_results (room_id, match_version);

-- ---------------------------------------------------------------------------
-- 2) bngc_ranked_matches: at most ONE open match per room, ever.
-- Enforces the tracker invariant (openMatch() refuses to open over an open
-- match) at the database so a racing writer cannot fork a room's version
-- sequence. Dirty data would fail the index build, so first mark every open
-- row except the newest per room as 'backed_out' (evidence preserved,
-- backout semantics match /api/ranked/room/backout).
WITH ranked_opens AS (
  SELECT id,
         ROW_NUMBER() OVER (
           PARTITION BY room_id
           ORDER BY match_version DESC, sealed_at DESC NULLS LAST, id DESC
         ) AS rn
  FROM public.bngc_ranked_matches
  WHERE status = 'open'
)
UPDATE public.bngc_ranked_matches m
SET status = 'backed_out'
FROM ranked_opens r
WHERE m.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS bngc_ranked_matches_one_open_per_room_uidx
  ON public.bngc_ranked_matches (room_id) WHERE status = 'open';

-- ---------------------------------------------------------------------------
-- 3-7) bngc_ranked_events: per-seal idempotency for the event bank.
-- The events table must NOT be content-unique: a genuine re-kill (A kills B,
-- B respawns via stG=2, A kills B again) repeats the identical
-- (killer, victim, etype) signature and is real evidence. Instead each seal
-- gets a stable identity (seal_id) and every event a stable position
-- (event_seq = index in the seal's immutable events array). A retried seal
-- replays the exact same (room_id, match_version, seal_id, event_seq) keys,
-- so the unique index absorbs duplicates while fresh evidence banks freely.
-- Legacy rows (seal_id IS NULL) are ignored by the partial index — history
-- needs no backfill and cannot be broken by this change.
ALTER TABLE public.bngc_ranked_events ADD COLUMN IF NOT EXISTS seal_id text;
ALTER TABLE public.bngc_ranked_events ADD COLUMN IF NOT EXISTS event_seq integer;
ALTER TABLE public.bngc_ranked_matches  ADD COLUMN IF NOT EXISTS seal_id text;

CREATE UNIQUE INDEX IF NOT EXISTS bngc_ranked_events_seal_seq_uidx
  ON public.bngc_ranked_events (room_id, match_version, seal_id, event_seq)
  WHERE seal_id IS NOT NULL;

-- Lookup support for reconciler-style sweeps that walk seals by id.
CREATE INDEX IF NOT EXISTS bngc_ranked_matches_seal_id_idx
  ON public.bngc_ranked_matches (seal_id) WHERE seal_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Admin queries (read-only, run before or after the migration):
--
-- Pre-existing duplicate event rows per (room, version) — the noise the
-- seal_id index absorbs going forward:
--   SELECT room_id, match_version, etype, actor_name, target_name, COUNT(*)
--   FROM public.bngc_ranked_events
--   GROUP BY 1,2,3,4,5
--   HAVING COUNT(*) > 1;
--
-- Rooms with more than one open match (statement 2 collapses these to the
-- newest, marking the rest backed_out):
--   SELECT room_id, COUNT(*)
--   FROM public.bngc_ranked_matches
--   WHERE status = 'open'
--   GROUP BY 1 HAVING COUNT(*) > 1;

-- ---------------------------------------------------------------------------
-- Rollback (run only to revert; drop the newer items first):
-- DROP INDEX IF EXISTS public.bngc_ranked_matches_seal_id_idx;
-- DROP INDEX IF EXISTS public.bngc_ranked_events_seal_seq_uidx;
-- DROP INDEX IF EXISTS public.bngc_ranked_matches_one_open_per_room_uidx;
-- DROP INDEX IF EXISTS public.ranked_results_room_ver_uidx;
-- ALTER TABLE public.bngc_ranked_matches DROP COLUMN IF EXISTS seal_id;
-- ALTER TABLE public.bngc_ranked_events  DROP COLUMN IF EXISTS event_seq;
-- ALTER TABLE public.bngc_ranked_events  DROP COLUMN IF EXISTS seal_id;
