# ClippedNow.gg Patch Notes

## v2.36 — clips match the buffer again

- Fixed 76s files from a 30s buffer: sessions now roll cleanly at the
  window span, so a saved clip can never outlive its headers (no more
  long timelines that stall after seconds, no more 2-second previews).
  Kills right after a roll get shorter clips - same honest tradeoff as
  the classic versions, and the logged window length is now the truth.

- New governor watches the actual game framerate (via rAF cadence, ~zero
  cost). If the game holds under ~25fps for ~30s while capture settings
  are heavy, it toasts exactly what to lower (bitrate → FPS → watermark,
  in that order) - once per session, never changes settings by itself.
- Panel status now shows live game fps next to src/paint rates, so any
  future "laggy" report is diagnosable at a glance.

- Root cause of the "corrupted" wave: long sessions prune the rolling
  window, and the pruned part contained the file headers — saved slices
  were undecodable. The session head (~256KB) is now pinned and prepended
  (overlap-free, timestamps stay monotonic), so every clip carries headers.
- Nothing is discarded anymore: probe failures save the clip flagged with
  a ⚠ CHECK badge (library) and note (preview) instead of deleting it.
  Try VLC/download on flagged clips.

## v2.33 — self-healing encoder fallback

- New cadence watchdog: ~7s into each session, if a starved encoder is
  delivering <0.4 chunks/sec on MP4 (output would be undecodable, not just
  choppy), the recorder flips that machine to WebM automatically and
  restarts the session. WebM tolerates sparse encoder output and still
  plays. Changing capture settings re-arms MP4 evaluation.

- The "Clip corrupted - discarded" wave was a false positive: a sliced MP4
  head can miss its moov box and fail decoding while the full file is
  perfect. Verify is now two-stage (fast head probe, full-file retry on
  head error) — discards only happen when the complete file actually
  fails to decode.

- Decode probe now reports *why* it failed: real decode errors still
  discard the clip, but slow-decode **timeouts save the file anyway**
  (timeouts lie under load — the file is usually fine) with an
  "unverified, check it plays" note.
- Library evicts by **bytes too** (~600MB cap alongside the 20-clip cap),
  oldest-first, sparing favorites and the just-saved clip — Beast bitrates
  can no longer silently fill browser storage and kill every later save.
- Storage write failures now toast loudly instead of a fake "saved".

## v2.30 — The smooth-clips update

**Recording that plays back right**
- Clips now record as one continuous session — no more stitching segments
  together, which was the cause of 2-3fps stutter files. What you record at
  30fps plays at 30fps.
- MP4/H264 output again (hardware-decoded everywhere: Chrome, VLC, Discord,
  iOS), with WebM available in Settings if you scrub timelines a lot.
- New **Format** setting: MP4 (smooth playback) vs WebM (precise seeking).
- New FPS options: **120** and **Unlimited** (source frame rate), plus
  **Source (~20Mbps)** and **Pro (30Mbps)** quality tiers and a
  **Beast mode** preset for high-end PCs (i9 + 4080/5070-class).
- Automatic PC detection recommends the right preset for your hardware
  (CPU threads, RAM, GPU) — one click to apply, never forced.

**Smarter clipping**
- Kill **streaks**: doubles/triples save purple STREAK clips with a mid-game toast.
- **Knock confirms**: knock someone and a teammate finishes them — still clips.
- Kills and deaths now count even when a name comes through blank (zone deaths included).
- Free Mode (DF) detection fixed — no more stuck-on-Lobby.
- Every save toast has a **Watch** button jumping straight to your clips.

**Editor (✂ Edit clip button in every preview)**
- Draggable **timeline** with filmstrip, start/end sliders, live playhead.
- Trim + download, one-click **Copy** (paste straight into Discord),
  **GIF export** for 1–5s moments (built-in encoder, nothing uploaded),
  save trims back to the library.
- Cancellable **export progress bar** floating above everything.

**Discord webhooks**
- Paste a webhook URL, see live online/offline status with avatar + name.
- **Auto-send** new clips by trigger type (kills, deaths, wins, streaks…),
  20MB cap, `clip-by-you` filenames, `clip from You - Date` messages.
- Failed uploads always keep the clip in your library.

**Performance & reliability**
- Near-zero game FPS impact: cached settings, throttled meters/composite,
  lazy library thumbnails (8 live max), backed-off idle tickers.
- False "Clip corrupted" discards fixed (head-probe verify, size-independent).
- Resize/reboot storms debounced — the buffer survives loading screens.
- Live capture diagnostics in the status line (`src 30fps`, paint rate,
  chunk cadence) so any future stutter is diagnosable in seconds.

100% local by default — clips never leave your PC unless you send them
through your own webhook.
