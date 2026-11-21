# Mirlos — song requests for house-party DJ sets

Guests request songs from their phones; Mirlos downloads them with yt-dlp and
exposes them to rekordbox through a `rekordbox.xml` bridge so the DJ can drag
them onto a deck (which imports + auto-analyzes them).

## Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Venue | House parties. YouTube-quality audio is acceptable. |
| 2 | Approval | None. Requests download immediately and go straight into the playlists. |
| 3 | rekordbox ingest | `rekordbox.xml` bridge. rekordbox has no watch-folder import; dragging from the XML pane imports + analyzes (Preferences → Analysis → Auto Analysis on). Never write `master.db`. |
| 4 | Access | LAN + QR by default. Optional Tailscale Funnel (free on Personal plan) toggled from `/dj`; when on, everyone needs the event code (embedded in the QR). |
| 5 | Search | Paste a YouTube link, or search and pick from the top 5 (`ytsearch5`). |
| 6 | Code layout | Shared `ytfetch` package (yt-dlp wrapper) used by Mirlos and later SampleMiner. |
| 7 | Format | `bestaudio[ext=m4a]` — native AAC, no transcode; tags + artwork embedded. |
| 8 | Guest limits | Display name (cookie), no cooldown, duplicates merge into +1 votes, tracks > 10 min rejected, DJ pause switch. |
| 9 | Playlists | Two XML playlists: `MIRLOS · NEW` (arrival order) and `MIRLOS · TOP` (votes desc, then arrival). Public live queue with upvotes. |
| 10 | Played detection | Poll rekordbox history read-only via pyrekordbox every 30 s; match by file path. Played tracks leave both playlists. |
| 11 | Stack | FastAPI + Jinja/HTMX + SQLite, single process on the Windows DJ laptop. htmx vendored (works without internet). |
| 12 | Moderation | `/dj` behind a PIN: delete request, ban guest, pause/resume, toggle Funnel, show QR. |
| 13 | Name | Mirlos. |
| 14 | DJ remix finder | Target = last played track's BPM/key from rekordbox history, or a sticky manual override. Top 8 results downloaded in full and analyzed (SampleMiner grid/key); ranked by BPM fit (×1, ×2, ×½; ≤6% fits, ≤10% close) then Camelot key relation. Genre chips build the query. |
| 15 | Guest BPM hints | DJ toggle (default on). "~N BPM" now-playing card; each search result analyzed from a 45 s clip and badged Fits now / Close / Slower / Faster. Guest clip backlog capped at 15. |

## Request lifecycle

```
submit ─► queued ─► downloading ─► ready ─► played
                         │                    ▲
                         └─► failed           │ (rekordbox history / DJ button)
any state ─► deleted (DJ)
```

Only `ready` requests appear in the XML. The XML is rewritten atomically
(`tmp` + `os.replace`) on every state change that affects it.

## Security model

- Only YouTube hosts accepted (`youtube.com`, `m.youtube.com`, `music.youtube.com`, `youtu.be`);
  video IDs validated against `[A-Za-z0-9_-]{11}` and download URLs rebuilt from the ID,
  so guests can't point yt-dlp at arbitrary extractors/hosts.
- Guest identity = random cookie token; names length-capped and auto-escaped.
- `/dj` PIN compared with `hmac.compare_digest`; session is a signed cookie (SameSite=Lax).
- Funnel mode requires the event code for every guest.
- No write access to rekordbox's database, ever.

## Risks / open items

1. pyrekordbox must be able to unlock your rekordbox version's `master.db`. If not, the
   poller disables itself and `/dj` has a manual **played** button.
2. rekordbox may cache `rekordbox.xml`; the XML pane's refresh button should reload it —
   verify on first run (Preferences → Advanced → Database → rekordbox xml → Imported Library).
3. No approval: the only filters are the duration cap and DJ moderation.

## Out of scope (v1)

Paid gigs/licensing, streaming services, approval queue, live deck BPM (Pro DJ Link / MIDI clock).
