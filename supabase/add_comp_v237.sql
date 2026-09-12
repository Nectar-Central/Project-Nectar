-- Comp match roster removal (leave detection / add-back), v237.
-- Players who leave mid-match are stored here and ignored in placement
-- and calculations until a host adds them back.
ALTER TABLE bngc_comp_matches
  ADD COLUMN IF NOT EXISTS removed_discord_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
