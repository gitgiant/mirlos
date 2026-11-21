import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mirlos import rbxml
from mirlos.app import create_app
from mirlos.config import Config
from mirlos import analysis
from mirlos.services import History, _norm
from ytfetch import Track

A, B, LONG = "aaaaaaaaaaa", "bbbbbbbbbbb", "lllllllllll"
TRACKS = {
    A: Track(A, "Song A", "Artist A", 200, None),
    B: Track(B, "Song B <script>", "Artist B", 180, None),
    LONG: Track(LONG, "Ten Hour Loop", "x", 36000, None),
}


class FakeFetcher:
    def __init__(self):
        self.downloads = []

    def search(self, q, n=5):
        return list(TRACKS.values())[:n]

    def resolve(self, vid):
        return TRACKS[vid]

    def download(self, vid, dest: Path) -> Path:
        self.downloads.append(vid)
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / f"{TRACKS[vid].title.split(' <')[0]} [{vid}].m4a"
        p.write_bytes(b"fake")
        return p

    def download_clip(self, vid, dest: Path, duration=None) -> Path:
        self.downloads.append(("clip", vid))
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / f"{vid}.clip.webm"
        p.write_bytes(b"fake")
        return p


BPMS = {A: (125.0, "9A"), B: (62.0, "8B"), LONG: (140.0, "3B")}


def fake_analyze(path, window=90.0):
    vid = Path(path).name.split("[")[-1].split("]")[0] if "[" in Path(path).name else Path(path).name[:11]
    bpm, key = BPMS[vid]
    return analysis.Result(bpm, key, 0.9)


@pytest.fixture
def env(tmp_path):
    played: set[str] = set()
    cfg = Config(data_dir=tmp_path, dj_pin="123456")
    f = FakeFetcher()
    last = {"title": "Nightline", "artist": "Glass Harbor", "bpm": 124.0, "key": "Am"}
    app = create_app(cfg, fetcher=f, start_services=False, history_source=lambda: History(played, last),
                     analyze_fn=fake_analyze)
    with TestClient(app) as c:
        yield c, app.state, f, played


def join(c, name="Ana"):
    r = c.post("/join", data={"name": name}, follow_redirects=False)
    assert r.status_code == 303
    return c


def drain(state):
    an = state.an
    while not (state.dl.q.empty() and an.dj.empty() and an.guest.empty()):
        if not state.dl.q.empty():
            state.dl.process(state.dl.q.get())
        if not an.dj.empty():
            an.process(an.dj.get(), "full")
        if not an.guest.empty():
            an.process(an.guest.get(), "clip")


def xml_playlists(state):
    root = ET.parse(state.cfg.xml_path).getroot()
    pls = {n.get("Name"): [t.get("Key") for t in n] for n in root.iter("NODE") if n.get("Type") == "1"}
    locs = {t.get("TrackID"): t.get("Location") for t in root.find("COLLECTION")}
    return pls, locs


def test_gate_then_request_by_link_downloads_into_xml(env):
    c, state, f, _ = env
    assert "Your name" in c.get("/").text
    join(c)
    r = c.post("/search", data={"q": f"https://youtu.be/{A}?si=x"})
    assert "Requested: Song A" in r.text and r.headers["HX-Trigger"] == "queue-changed"
    pls, _ = xml_playlists(state)
    assert pls[rbxml.PLAYLIST_NEW] == []  # not ready yet
    drain(state)
    assert f.downloads == [A]
    pls, locs = xml_playlists(state)
    assert pls[rbxml.PLAYLIST_NEW] == ["1"] and locs["1"].startswith("file://localhost/")
    assert "%20" in locs["1"]  # spaces encoded


def test_search_results_and_duration_cap(env):
    c, state, _, _ = env
    join(c)
    r = c.post("/search", data={"q": "song"})
    assert "Song A" in r.text and "Too long" in r.text
    assert "&lt;script&gt;" in r.text and "<script>" not in r.text  # escaped
    r = c.post("/request", data={"video_id": LONG})
    assert "Too long" in r.text
    assert state.store.all() == []


def test_non_youtube_and_bad_ids_rejected(env):
    c, state, _, _ = env
    join(c)
    assert "Only YouTube" in c.post("/search", data={"q": "https://evil.example/x"}).text
    assert "Bad request" in c.post("/request", data={"video_id": "../../etc"}).text
    assert state.store.all() == []


def test_dedupe_votes_and_top_order(env):
    c, state, _, _ = env
    join(c, "Ana").post("/request", data={"video_id": A})
    c.post("/request", data={"video_id": B})
    drain(state)
    c2 = TestClient(c.app)
    join(c2, "Ben")
    assert "+1" in c2.post("/request", data={"video_id": B}).text
    assert state.store.by_video(B).votes == 2
    c2.post(f"/vote/{state.store.by_video(B).id}")  # second vote from same guest is ignored
    assert state.store.by_video(B).votes == 2
    pls, _ = xml_playlists(state)
    assert pls[rbxml.PLAYLIST_NEW] == ["1", "2"]
    assert pls[rbxml.PLAYLIST_TOP] == ["2", "1"]


def test_history_marks_played_and_removes_from_xml(env):
    c, state, _, played = env
    join(c).post("/request", data={"video_id": A})
    drain(state)
    played.add(_norm(state.store.by_video(A).path))
    assert state.hist.poll_once() == 1
    assert state.store.by_video(A).status == "played"
    pls, _ = xml_playlists(state)
    assert pls[rbxml.PLAYLIST_NEW] == []
    assert "already been played" in c.post("/request", data={"video_id": A}).text


def test_dj_auth_delete_ban_pause(env):
    c, state, f, _ = env
    join(c, "Troll").post("/request", data={"video_id": A})
    drain(state)
    assert c.post("/dj/delete", data={"rid": 1}, follow_redirects=False).headers["location"] == "/dj"
    assert state.store.by_video(A).status == "ready"  # not logged in → no-op
    assert c.post("/dj/login", data={"pin": "000000"}).status_code == 403
    c.post("/dj/login", data={"pin": "123456"})
    assert "Requests" in c.get("/dj").text
    c.post("/dj/delete", data={"rid": 1})
    assert state.store.by_video(A).status == "deleted"
    assert xml_playlists(state)[0][rbxml.PLAYLIST_NEW] == []
    assert "removed" in c.post("/request", data={"video_id": A}).text
    c.post("/dj/pause")
    assert "paused" in c.post("/request", data={"video_id": B}).text
    c.post("/dj/pause")
    tok = state.store.guests()[0]["token"]
    c.post("/dj/ban", data={"token": tok})
    assert "closed" in c.post("/request", data={"video_id": B}).text


def test_pin_lockout(env):
    c, *_ = env
    for _ in range(5):
        c.post("/dj/login", data={"pin": "nope"})
    assert c.post("/dj/login", data={"pin": "123456"}).status_code == 429


def test_event_code_required_when_funnel_on(env):
    c, state, _, _ = env
    join(c)
    state.store.set("funnel", "1")
    state.store.set("event_code", "K7QX")
    assert "Event code" in c.get("/").text
    assert c.post("/search", data={"q": "x"}).status_code == 401
    assert c.post("/join", data={"name": "Ana", "code": "nope"}).status_code == 403
    assert c.post("/join", data={"name": "Ana", "code": "ünï"}).status_code == 403  # non-ascii safe
    c.get("/?code=k7qx")
    assert "Hi Ana" in c.get("/").text


def test_failed_download_can_retry(env):
    c, state, f, _ = env
    join(c).post("/request", data={"video_id": A})
    f.download = lambda vid, dest: (_ for _ in ()).throw(RuntimeError("403"))
    drain(state)
    assert state.store.by_video(A).status == "failed"
    assert "Retrying" in c.post("/request", data={"video_id": A}).text


def test_location_windows_style():
    loc = rbxml.location("/tmp/a b&c.m4a")
    assert loc == "file://localhost/tmp/a%20b%26c.m4a"


def test_remix_query_strips_junk():
    from mirlos.app import remix_query

    assert remix_query("Daft Punk - One More Time (Official Video) [4K]") == "Daft Punk - One More Time remix"
    assert remix_query("Song (feat. X) [Official Audio]") == "Song (feat. X) remix"
    assert remix_query("Track | Lyrics Video") == "Track | Lyrics Video remix"


def test_dj_remix_search_and_add(env):
    c, state, f, _ = env
    state.hist.poll_once()
    queries = []
    f.search = lambda q, n=5: (queries.append(q), list(TRACKS.values())[:n])[1]
    join(c).post("/request", data={"video_id": A})
    assert c.get("/dj/search?rid=1", follow_redirects=False).status_code == 303  # needs DJ
    c.post("/dj/login", data={"pin": "123456"})
    assert "Remixes" in c.get("/dj").text
    r = c.get("/dj/search?rid=1")
    assert queries[-1] == "Song A remix" and "bootleg" in r.text and "/dj/cands?ids=" in r.text
    c.post("/dj/pause")  # DJ adds bypass pause and the duration cap
    r = c.post("/dj/add", data={"video_id": LONG, "q": "Song A remix"}, follow_redirects=False)
    assert r.headers["location"] == "/dj/search?q=Song%20A%20remix"
    req = state.store.by_video(LONG)
    assert req.guest_name == "DJ" and req.status == "queued"  # added before its analysis finished
    drain(state)
    assert state.store.by_video(LONG).status == "ready"
    c.post("/dj/delete", data={"rid": req.id})
    c.post("/dj/add", data={"video_id": LONG})  # DJ can restore a deleted track
    assert state.store.by_video(LONG).status == "ready"  # analyzed copy reused, no re-download


def test_camelot_and_key_relation():
    assert analysis.camelot("Am") == "8A" and analysis.camelot("C") == "8B"
    assert analysis.camelot("F#m") == "11A" and analysis.camelot("Gbm") == "11A"
    assert analysis.camelot("Db") == "3B" and analysis.camelot("12a") == "12A"
    assert analysis.camelot("nonsense") is None
    assert analysis.key_relation("8A", "Am")[1] == "same key"
    assert analysis.key_relation("9A", "8A")[1] == "compatible"
    assert analysis.key_relation("12A", "1A")[1] == "compatible"
    assert analysis.key_relation("8B", "8A")[1] == "relative"
    assert analysis.key_relation("10A", "8A")[1] == "+2 steps"
    assert analysis.key_relation("3B", "8A")[1] == "clash"


def test_fit():
    assert analysis.fit(125, 124).cls == "fits" and analysis.fit(125, 124).label == "+0.8%"
    assert analysis.fit(124, 124).label == "on target"
    h = analysis.fit(62, 124)
    assert h.cls == "fits" and h.label == "half-time ×2"
    assert analysis.fit(174, 87).label == "double-time ×½"
    assert analysis.fit(135, 124).cls == "close"
    assert analysis.fit(100, 124).cls == "slower" and analysis.fit(100, 124).guest_label == "Slower"
    assert analysis.fit(150, 124).cls == "faster"
    assert analysis.fit(None, 124).cls == "unknown"


def test_guest_bpm_hints(env):
    c, state, f, _ = env
    state.hist.poll_once()
    join(c)
    assert "~124 BPM" in c.get("/now").text
    r = c.post("/search", data={"q": "song"})
    assert r.text.count('hx-get="/bpm/') == 2  # LONG is over the cap: no analysis
    assert "BPM…" in c.get(f"/bpm/{A}").text
    drain(state)
    assert ("clip", A) in f.downloads and not any(state.an.clips_dir.iterdir())  # clips deleted
    a = c.get(f"/bpm/{A}").text
    assert "~125 BPM" in a and "Fits now" in a
    assert "Fits now" in c.get(f"/bpm/{B}").text  # 62 → half-time fits
    c.post("/dj/login", data={"pin": "123456"})
    c.post("/dj/guestbpm")
    assert c.get("/now").text == "" and c.get(f"/bpm/{A}").text == ""


def test_target_override_and_ranking(env):
    c, state, f, _ = env
    state.hist.poll_once()
    c.post("/dj/login", data={"pin": "123456"})
    assert "Last played: Glass Harbor — Nightline" in c.get("/dj").text
    c.get("/dj/search?q=anything")
    drain(state)
    rows = c.get(f"/dj/cands?ids={A},{B},{LONG}").text
    assert rows.index("Song B") < rows.index("Song A") < rows.index("Ten Hour")  # 62×2 = 124 exact beats +0.8%
    assert "+0.8%" in rows and "half-time" in rows and "compatible" in rows
    c.post("/dj/target", data={"bpm": "140", "key": "3B", "back_url": "/dj"})
    assert state.target.get()["bpm"] == 140.0
    rows = c.get(f"/dj/cands?ids={A},{B},{LONG}").text
    assert rows.index("Ten Hour") < rows.index("Song A") and "same key" in rows
    assert c.post("/dj/target", data={"bpm": "9999"}, follow_redirects=False).headers["location"].startswith("/dj?err=")
    c.post("/dj/target", data={"bpm": ""})
    assert state.target.get()["source"] == "last played"


def test_dj_add_analyzed_candidate_is_instant(env):
    c, state, f, _ = env
    c.post("/dj/login", data={"pin": "123456"})
    c.get("/dj/search?q=anything")
    drain(state)
    n = len(f.downloads)
    c.post("/dj/add", data={"video_id": A, "q": "anything"})
    r = state.store.by_video(A)
    assert r.status == "ready" and Path(r.path).parent == state.cfg.requests_dir and Path(r.path).exists()
    assert len(f.downloads) == n  # no second download
    assert xml_playlists(state)[0][rbxml.PLAYLIST_NEW] == [str(r.id)]
    c.post("/dj/reset")
    assert Path(r.path).exists()  # used file kept
    assert not any(state.an.cands_dir.iterdir())  # unused candidates purged
