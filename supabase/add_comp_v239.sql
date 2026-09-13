-- Comp manual point awards + manual PR flag (v239).
-- point_awards: per-match manual +/-X points {discord_id: pts}, folded in scoring.
-- manual: PR history rows set by host override (preserved on recompute).
ALTER TABLE bngc_comp_matches
  ADD COLUMN IF NOT EXISTS point_awards jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE bngc_comp_pr_history
  ADD COLUMN IF NOT EXISTS manual boolean NOT NULL DEFAULT false;
