-- Add host_status column to ranked_rooms (is_host_playing | hosting_only)
ALTER TABLE ranked_rooms
ADD COLUMN IF NOT EXISTS host_status text NOT NULL DEFAULT 'is_host_playing';
