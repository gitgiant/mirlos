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
git clone https://github.com/gitgiant/mirlos
cd mirlos
uv sync --extra rekordbox
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
ban guests (drops their unplayed requests), **New night** (clears requests; files are kept).

**Remixes** on any request searches YouTube for `<title> remix` (junk like *(Official Video)* stripped),
with one-tap variants: edit, bootleg, VIP, extended mix, instrumental. A free search box sits above
the table. DJ adds skip pause and the duration cap, and can bring back tracks you deleted earlier.

## Config (env vars)

| Var | Default |
|---|---|
| `MIRLOS_DATA_DIR` | `~/Mirlos` (db, `requests/`, `rekordbox.xml`) |
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
