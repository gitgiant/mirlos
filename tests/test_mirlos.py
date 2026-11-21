import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mirlos import rbxml
from mirlos.app import create_app
from mirlos.config import Config
from mirlos.services import _norm
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
        p = dest / f"{TRACKS[vid].title.split(' <')[0]} [{vid}].m4a"
        p.write_bytes(b"fake")
        return p


@pytest.fixture
def env(tmp_path):
    played: set[str] = set()
    cfg = Config(data_dir=tmp_path, dj_pin="123456")
    f = FakeFetcher()
    app = create_app(cfg, fetcher=f, start_services=False, history_source=lambda: played)
    with TestClient(app) as c:
        yield c, app.state, f, played


def join(c, name="Ana"):
    r = c.post("/join", data={"name": name}, follow_redirects=False)
    assert r.status_code == 303
    return c


def drain(state):
    while not state.dl.q.empty():
        state.dl.process(state.dl.q.get())


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
    queries = []
    f.search = lambda q, n=5: (queries.append(q), list(TRACKS.values())[:n])[1]
    join(c).post("/request", data={"video_id": A})
    assert c.get("/dj/search?rid=1", follow_redirects=False).status_code == 303  # needs DJ
    c.post("/dj/login", data={"pin": "123456"})
    assert "Remixes" in c.get("/dj").text
    r = c.get("/dj/search?rid=1")
    assert queries[-1] == "Song A remix" and "Song B" in r.text and "bootleg" in r.text
    c.post("/dj/pause")  # DJ adds bypass pause and the duration cap
    r = c.post("/dj/add", data={"video_id": LONG, "q": "Song A remix"}, follow_redirects=False)
    assert r.headers["location"] == "/dj/search?q=Song%20A%20remix"
    req = state.store.by_video(LONG)
    assert req.guest_name == "DJ" and req.status == "queued"
    drain(state)
    assert state.store.by_video(LONG).status == "ready"
    c.post("/dj/delete", data={"rid": req.id})
    c.post("/dj/add", data={"video_id": LONG})  # DJ can restore a deleted track
    assert state.store.by_video(LONG).status == "queued"
