# Mirlos

Song requests for DJ sets. Guests scan a QR code, search or paste a YouTube link,
and Mirlos downloads the track (native m4a, no transcode) and exposes it to
rekordbox through `rekordbox.xml` playlists **MIRLOS · NEW** (arrival order) and
**MIRLOS · TOP** (by votes). Tracks drop out automatically once they show up in
your rekordbox play history. Design: [docs/SPEC.md](docs/SPEC.md).

## Setup (Windows DJ laptop)

```powershell
winget install Gyan.FFmpeg DenoLand.Deno astral-sh.uv   # ffmpeg: tags/artwork; deno: yt-dlp's YouTube JS runtime
git clone https://github.com/gitgiant/ytfetch
git clone https://github.com/gitgiant/sampleminer      # BPM/key analysis
git clone https://github.com/gitgiant/mirlos
cd mirlos
uv sync --extra rekordbox --extra analysis
$env:MIRLOS_DJ_PIN = "your-pin"     # otherwise one is generated and printed
uv run mirlos
```

Allow port 8080 through Windows Firewall on **Private** networks when prompted.

### rekordbox (one time)

1. **Preferences → Analysis → Track Analysis**: turn on *Auto Analysis* (BPM, key, phrase as you like).
2. **Preferences → Advanced → Database → rekordbox xml → Imported Library**: point it at
   `%USERPROFILE%\Mirlos\rekordbox.xml` (printed at startup).
3. **Preferences → View → Layout**: enable *rekordbox xml* so it shows in the tree.

During the set: open **rekordbox xml → MIRLOS · NEW / TOP**, hit refresh, and drag a track to
a deck or the collection. That imports and analyzes it.

### Funnel (optional, for guests on cellular)

Enable HTTPS + Funnel for the laptop in the Tailscale admin console once. Then use the
**Funnel** toggle on `/dj`. It generates a 4-letter event code (embedded in both QRs) that
everyone needs while Funnel is on.

## DJ page — `/dj`

PIN-protected: QR codes, pause/resume, Funnel toggle, delete / mark played / retry requests,
ban guests (drops their unplayed requests), **Clear** (clears requests; files are kept).

### Remix finder (BPM-matched)

The **target** is the BPM/key of the last track in your rekordbox history (refreshed every
30 s), or a manual override that sticks until you hit *Use last played*.

**Remixes** on any request searches YouTube for `<title> remix` (junk like *(Official Video)* stripped),
with one-tap variants: edit, bootleg, VIP, extended mix, instrumental. A free search box sits above
the table. The top 8 results are downloaded in full and analyzed (BPM + Camelot key) in the
background, then ranked against the target: **on target / ±x%** (≤6% fits, ≤10% close),
**half-time ×2 / double-time ×½**, and key **same / compatible / relative / +2 steps / clash**.
**Add** on an analyzed candidate is instant (the file is already downloaded). DJ adds skip pause
and the duration cap, and can bring back tracks you deleted earlier. *Clear* deletes unused
candidates.

### Guest BPM hints

Off by default. With **Guest BPM hints** turned on (toggle on `/dj`), guests see "The DJ is playing around ~124 BPM", and each
search result gets a rough BPM from a 45 s clip plus a **Fits now / Close / Slower / Faster**
badge. Clips are deleted after analysis; at most 15 guest clips queue at once. Half/double-time
misreads are possible on clips.

## Config (env vars)

| Var | Default |
|---|---|
| `MIRLOS_DATA_DIR` | `~/Mirlos` (db, `rekordbox.xml`, PIN) |
| `MIRLOS_MUSIC_DIR` | `~/Music/mirlos` (downloaded requests) |
| `MIRLOS_PORT` / `MIRLOS_HOST` | `8080` / `0.0.0.0` |
| `MIRLOS_DJ_PIN` | generated, saved in `dj_pin.txt` |
| `MIRLOS_MAX_DURATION` | `600` s |
| `MIRLOS_WORKERS` | `2` parallel downloads |
| `MIRLOS_HISTORY_POLL` | `30` s |
| `MIRLOS_XML_PATH` | `<data>/rekordbox.xml` |

## First-run checks

- `/dj` shows `History poller: ok` after ~30 s. If it shows an error, pyrekordbox can't unlock
  your `master.db` — use the **Played** button instead.
- After a download, refresh the rekordbox xml pane. If the new track doesn't appear,
  rekordbox is caching the XML; re-select the Imported Library file or restart rekordbox.

## Dev

```
uv run pytest
```
