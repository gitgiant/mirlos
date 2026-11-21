from __future__ import annotations

import hmac
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

import segno
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import ytfetch

from .config import Config
from . import analysis
from .services import (_APPROVE_RE, Analyzer, Downloader, Fetcher, Funnel, FunnelNeedsApproval, HistoryPoller,
                       Library, Target, _norm, lan_ip)
from .store import Store

HERE = Path(__file__).parent
GUEST_COOKIE, CODE_COOKIE = "mirlos_guest", "mirlos_code"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


DJ_TOKEN = "dj"  # can't collide: guest tokens are 22-char random
_JUNK = re.compile(r"\s*[\(\[][^\)\]]*\b(official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|mv|m/v)\b[^\)\]]*[\)\]]",
                   re.I)


def remix_query(title: str) -> str:
    """'Artist - Song (Official Video) [4K]' → 'Artist - Song remix'"""
    t = _JUNK.sub("", title)
    t = re.sub(r"\s+", " ", t).strip(" -|")
    return f"{t} remix"


def _clean_name(s: str) -> str:
    return _CTRL.sub("", s).strip()[:32]


class TrackCache(OrderedDict):
    """Recently seen search results, so picking one doesn't need a second lookup."""

    def put(self, t) -> None:
        self[t.video_id] = t
        self.move_to_end(t.video_id)
        while len(self) > 500:
            self.popitem(last=False)


def create_app(cfg: Config | None = None, fetcher: Fetcher | None = None,
               start_services: bool = True, history_source=None, analyze_fn=None) -> FastAPI:
    cfg = cfg or Config()
    fetcher = fetcher or ytfetch
    store = Store(cfg.db_path)
    lib = Library(cfg, store)
    dl = Downloader(cfg, store, lib, fetcher)
    hist = HistoryPoller(cfg, store, lib, **({"source": history_source} if history_source else {}))
    funnel = Funnel(cfg.port)
    an = Analyzer(cfg, store, fetcher, run=analyze_fn)
    target = Target(store, hist)
    cache = TrackCache()
    pin_fails = {"n": 0, "until": 0.0}

    @asynccontextmanager
    async def lifespan(_app):
        lib.refresh()
        if start_services:
            dl.start()
            hist.start()
            an.start()
        yield

    app = FastAPI(title="Mirlos", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, session_cookie="mirlos_dj",
                       same_site="lax", max_age=12 * 3600)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    app.state.__dict__.update(cfg=cfg, store=store, lib=lib, dl=dl, hist=hist, funnel=funnel, an=an, target=target)

    def render(req: Request, name: str, status: int = 200, headers: dict | None = None, **ctx) -> HTMLResponse:
        ctx.setdefault("paused", store.paused)
        return tpl.TemplateResponse(req, name, ctx, status_code=status, headers=headers)

    # ---------------------------------------------------------------- guests
    def code_required() -> str | None:
        return store.get("event_code") if store.get("funnel") == "1" else None

    def current_guest(req: Request):
        code = code_required()
        if code and not _eq(req.cookies.get(CODE_COOKIE, ""), code):
            return None
        tok = req.cookies.get(GUEST_COOKIE, "")
        return store.guest(tok) if tok else None

    def message(req: Request, text: str, ok: bool = True) -> HTMLResponse:
        return render(req, "_msg.html", text=text, ok=ok, headers={"HX-Trigger": "queue-changed"})

    @app.get("/", response_class=HTMLResponse)
    def index(req: Request, code: str = ""):
        need = code_required()
        if need and code and _eq(code.strip().upper(), need):
            r = RedirectResponse("/", 303)
            r.set_cookie(CODE_COOKIE, need, max_age=86400, httponly=True, samesite="lax")
            return r
        g = current_guest(req)
        if g is None:
            have_code = not need or _eq(req.cookies.get(CODE_COOKIE, ""), need)
            return render(req, "join.html", need_code=not have_code)
        return render(req, "index.html", guest=g, max_min=cfg.max_duration // 60,
                      hints=store.guest_bpm and an.enabled)

    @app.post("/join")
    def join(req: Request, name: str = Form(...), code: str = Form("")):
        need = code_required()
        resp = RedirectResponse("/", 303)
        if need:
            given = code.strip().upper() or req.cookies.get(CODE_COOKIE, "")
            if not _eq(given, need):
                return render(req, "join.html", need_code=True, error="Wrong event code", status=403)
            resp.set_cookie(CODE_COOKIE, need, max_age=86400, httponly=True, samesite="lax")
        name = _clean_name(name)
        if not name:
            return render(req, "join.html", need_code=bool(need), error="Pick a name", status=400)
        tok = req.cookies.get(GUEST_COOKIE) or secrets.token_urlsafe(16)
        store.upsert_guest(tok, name)
        resp.set_cookie(GUEST_COOKIE, tok, max_age=7 * 86400, httponly=True, samesite="lax")
        return resp

    def add_request(req: Request, g, video_id: str) -> HTMLResponse:
        if g["banned"]:
            return message(req, "Requests are closed.", ok=False)
        if store.paused:
            return message(req, "Requests are paused right now.", ok=False)
        t = cache.get(video_id)
        if t is None or t.duration is None:
            try:
                t = fetcher.resolve(video_id)
            except Exception:
                return message(req, "Couldn't find that video.", ok=False)
        if t.duration and t.duration > cfg.max_duration:
            return message(req, f"Too long — max {cfg.max_duration // 60} minutes.", ok=False)
        outcome, rid = store.submit(video_id=t.video_id, title=t.title, uploader=t.uploader,
                                    duration=t.duration, thumbnail=t.thumbnail, guest=g["token"])
        if outcome in ("added", "retry"):
            dl.enqueue(rid)
        elif outcome == "voted":
            lib.refresh()
        text = {"added": f"Requested: {t.title}", "retry": f"Retrying: {t.title}",
                "voted": f"Already requested — added your +1: {t.title}",
                "played": "That one's already been played tonight.",
                "blocked": "The DJ removed that one."}[outcome]
        return message(req, text, ok=outcome in ("added", "retry", "voted"))

    @app.post("/search", response_class=HTMLResponse)
    def search(req: Request, q: str = Form("")):
        g = current_guest(req)
        if g is None:
            return Response(status_code=401, headers={"HX-Redirect": "/"})
        q = q.strip()[:200]
        if not q:
            return HTMLResponse("")
        vid = ytfetch.parse_video_id(q)
        if vid:
            return add_request(req, g, vid)
        if "://" in q:
            return message(req, "Only YouTube links work.", ok=False)
        try:
            results = fetcher.search(q, 5)
        except Exception:
            return message(req, "Search failed — try again.", ok=False)
        hints = store.guest_bpm and an.enabled
        for t in results:
            cache.put(t)
            if hints and (t.duration or 0) <= cfg.max_duration:
                an.request(t, "clip")
        return render(req, "_results.html", results=results, max_dur=cfg.max_duration, hints=hints)

    @app.post("/request", response_class=HTMLResponse)
    def request_track(req: Request, video_id: str = Form(...)):
        g = current_guest(req)
        if g is None:
            return Response(status_code=401, headers={"HX-Redirect": "/"})
        try:
            ytfetch.watch_url(video_id)  # validates the id
        except Exception:
            return message(req, "Bad request.", ok=False)
        return add_request(req, g, video_id)

    @app.post("/vote/{rid}", response_class=HTMLResponse)
    def vote(req: Request, rid: int):
        g = current_guest(req)
        r = store.get_request(rid)
        if g is not None and not g["banned"] and r and r.status in ("queued", "downloading", "ready"):
            store.vote(rid, g["token"])
            if r.status == "ready":
                lib.refresh()
        return queue_partial(req)

    @app.get("/queue", response_class=HTMLResponse)
    def queue_partial(req: Request):
        g = current_guest(req)
        if g is None:
            return HTMLResponse("")
        mine = store.voted_by(g["token"])
        return render(req, "_queue.html", items=store.queue(), mine=mine)

    # ---------------------------------------------------------------- DJ
    def is_dj(req: Request) -> bool:
        return req.session.get("dj") is True

    def back() -> RedirectResponse:
        return RedirectResponse("/dj", 303)

    @app.get("/dj", response_class=HTMLResponse)
    def dj(req: Request, err: str = "", approve: str = ""):
        if not is_dj(req):
            return render(req, "dj_login.html")
        # only ever link to Tailscale's own admin console
        approve = approve if _APPROVE_RE.fullmatch(approve) else ""
        lan = f"http://{lan_ip()}:{cfg.port}/"
        fun_on = store.get("funnel") == "1"
        code = store.get("event_code")
        furl = funnel.url() if fun_on else None
        links = [("LAN", lan + (f"?code={code}" if fun_on else ""))]
        if furl:
            links.append(("Funnel", f"{furl}/?code={code}"))
        qrs = [(label, url, segno.make(url, error="m").svg_inline(scale=6, dark="#000", light="#fff"))
               for label, url in links]
        return render(req, "dj.html", items=store.all(), guests=store.guests(), qrs=qrs,
                      funnel=fun_on, code=code, hist=hist.status, xml=str(cfg.xml_path), err=err, approve=approve,
                      target=target.get(), guest_bpm=store.guest_bpm, analysis_on=an.enabled)

    @app.post("/dj/login")
    def dj_login(req: Request, pin: str = Form(...)):
        now = time.time()
        if now < pin_fails["until"]:
            return render(req, "dj_login.html", error="Locked — wait a minute.", status=429)
        if _eq(pin.strip(), cfg.dj_pin):
            pin_fails["n"] = 0
            req.session["dj"] = True
            return back()
        pin_fails["n"] += 1
        if pin_fails["n"] >= 5:
            pin_fails.update(n=0, until=now + 60)
        return render(req, "dj_login.html", error="Wrong PIN", status=403)

    STYLES = ["house", "tech house", "techno", "drum & bass", "amapiano", "hip hop", "baile funk",
              "jersey club", "uk garage", "edit", "bootleg", "VIP", "extended"]

    @app.get("/dj/search", response_class=HTMLResponse)
    def dj_search(req: Request, q: str = "", rid: int = 0, base: str = "", style: str = ""):
        if not is_dj(req):
            return RedirectResponse("/dj", 303)
        src = store.get_request(rid) if rid else None
        if src:
            base = base or remix_query(src.title)[: -len(" remix")]
        if base and not q:
            q = f"{base} {style} remix" if style and style not in ("edit", "bootleg", "VIP", "extended") \
                else f"{base} {style or 'remix'}"
        q = re.sub(r"\s+", " ", q).strip()[:200]
        results, err = [], ""
        if q:
            try:
                results = fetcher.search(q, 8)
            except Exception as e:
                err = f"Search failed: {str(e)[:120]}"
            for t in results:
                cache.put(t)
                an.request(t, "full")
        ids = ",".join(t.video_id for t in results)
        return render(req, "dj_search.html", q=q, base=base, style=style, rid=rid, src=src, ids=ids,
                      styles=STYLES, err=err, target=target.get(), analysis_on=an.enabled)

    def ranked(ids: list[str]):
        tgt = target.get() or {}
        have = {r.video_id: r.status for r in store.all()}
        rows = []
        for i, c in enumerate(store.candidates(ids, "full")):
            f = analysis.fit(c["bpm"], tgt.get("bpm"))
            krank, klabel = analysis.key_relation(c["key"], tgt.get("key"))
            order = {"fits": 0, "close": 1, "slower": 2, "faster": 2}.get(f.cls, 3)
            pending = c["status"] not in ("done", "failed")
            rows.append({"c": c, "fit": f, "klabel": klabel, "krank": krank, "pending": pending,
                         "have": have.get(c["video_id"]),
                         "sort": (pending, c["status"] == "failed", order, abs(f.pct), krank, i)})
        rows.sort(key=lambda r: r["sort"])
        return rows

    @app.get("/dj/cands", response_class=HTMLResponse)
    def dj_cands(req: Request, ids: str = "", q: str = ""):
        if not is_dj(req):
            return HTMLResponse("", status_code=401)
        vids = [v for v in ids.split(",") if ytfetch._ID_RE.match(v)][:20]
        rows = ranked(vids)
        done = sum(not r["pending"] for r in rows)
        return render(req, "_cands.html", rows=rows, done=done, total=len(rows), ids=ids, q=q,
                      polling=any(r["pending"] for r in rows), target=target.get())

    @app.post("/dj/add")
    def dj_add(req: Request, video_id: str = Form(...), q: str = Form(""), back_url: str = Form("")):
        if not is_dj(req):
            return RedirectResponse("/dj", 303)
        try:
            ytfetch.watch_url(video_id)
            t = cache.get(video_id) or fetcher.resolve(video_id)
        except Exception:
            return RedirectResponse("/dj?err=" + quote("Couldn't add that video"), 303)
        store.upsert_guest(DJ_TOKEN, "DJ")
        outcome, rid = store.submit(video_id=t.video_id, title=t.title, uploader=t.uploader,
                                    duration=t.duration, thumbnail=t.thumbnail, guest=DJ_TOKEN)
        if outcome == "blocked":  # DJ deleted it earlier; DJ can bring it back
            store.set_status(rid, "queued")
            outcome = "added"
        if outcome in ("added", "retry"):
            c = store.candidate(video_id, "full")
            if c and c["status"] == "done" and c["path"] and Path(c["path"]).is_file():
                dst = cfg.requests_dir / Path(c["path"]).name
                if not dst.exists():
                    shutil.move(c["path"], dst)
                store.update_candidate(video_id, "full", path=str(dst))
                store.set_status(rid, "ready", path=str(dst))
                lib.refresh()
            else:
                dl.enqueue(rid)
        nxt = back_url if back_url.startswith("/dj/search?") else "/dj/search?q=" + quote(q)
        return RedirectResponse(nxt, 303)

    @app.post("/dj/target")
    def dj_target(req: Request, bpm: str = Form(""), key: str = Form(""), back_url: str = Form("/dj")):
        if not is_dj(req):
            return RedirectResponse("/dj", 303)
        nxt = back_url if back_url.startswith("/dj") else "/dj"
        if not bpm.strip():
            target.clear()
        else:
            try:
                target.set(bpm, key)
            except ValueError as e:
                return RedirectResponse("/dj?err=" + quote(str(e)), 303)
        return RedirectResponse(nxt, 303)

    # ---------------------------------------------------------------- guest BPM hints
    @app.get("/now", response_class=HTMLResponse)
    def now_card(req: Request):
        if current_guest(req) is None or not store.guest_bpm:
            return HTMLResponse("")
        return render(req, "_now.html", target=target.get())

    @app.get("/bpm/{video_id}", response_class=HTMLResponse)
    def bpm_badge(req: Request, video_id: str):
        if current_guest(req) is None or not store.guest_bpm or not ytfetch._ID_RE.match(video_id):
            return HTMLResponse("")
        a = store.analysis(video_id)
        c = None if a else (store.candidate(video_id, "clip") or store.candidate(video_id, "full"))
        pending = a is None and c is not None and c["status"] not in ("failed", "done")
        tgt = target.get() or {}
        f = analysis.fit(a["bpm"] if a else None, tgt.get("bpm"))
        return render(req, "_bpm.html", vid=video_id, a=a, pending=pending, fit=f)

    @app.post("/dj/{action}")
    def dj_action(req: Request, action: str, rid: int = Form(0), token: str = Form("")):
        if not is_dj(req):
            return RedirectResponse("/dj", 303)
        if action == "logout":
            req.session.clear()
        elif action == "pause":
            store.set("paused", "0" if store.paused else "1")
        elif action == "delete":
            store.set_status(rid, "deleted")
            lib.refresh()
        elif action == "played":
            store.set_status(rid, "played")
            lib.refresh()
        elif action == "retry":
            store.set_status(rid, "queued")
            dl.enqueue(rid)
        elif action in ("ban", "unban"):
            store.ban(token, action == "ban")
            lib.refresh()
        elif action == "reset":
            keep = {_norm(r.path) for r in store.all() if r.path}
            store.reset_night()
            an.purge(keep)
            lib.refresh()
        elif action == "guestbpm":
            store.set("guest_bpm", "0" if store.guest_bpm else "1")
        elif action == "funnel":
            try:
                if store.get("funnel") == "1":
                    funnel.disable()
                    store.set("funnel", "0")
                else:
                    store.set("event_code", "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
                                                    for _ in range(4)))
                    funnel.enable()
                    store.set("funnel", "1")
            except FunnelNeedsApproval as e:
                return RedirectResponse("/dj?approve=" + quote(e.link, safe=""), 303)
            except Exception as e:
                return RedirectResponse("/dj?err=" + quote(f"Funnel: {str(e)[:120]}"), 303)
        else:
            return Response(status_code=404)
        return back()

    return app
