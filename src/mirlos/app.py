from __future__ import annotations

import hmac
import re
import secrets
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
from .services import Downloader, Fetcher, Funnel, HistoryPoller, Library, lan_ip
from .store import Store

HERE = Path(__file__).parent
GUEST_COOKIE, CODE_COOKIE = "mirlos_guest", "mirlos_code"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


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
               start_services: bool = True, history_source=None) -> FastAPI:
    cfg = cfg or Config()
    fetcher = fetcher or ytfetch
    store = Store(cfg.db_path)
    lib = Library(cfg, store)
    dl = Downloader(cfg, store, lib, fetcher)
    hist = HistoryPoller(cfg, store, lib, **({"source": history_source} if history_source else {}))
    funnel = Funnel(cfg.port)
    cache = TrackCache()
    pin_fails = {"n": 0, "until": 0.0}

    @asynccontextmanager
    async def lifespan(_app):
        lib.refresh()
        if start_services:
            dl.start()
            hist.start()
        yield

    app = FastAPI(title="Mirlos", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, session_cookie="mirlos_dj",
                       same_site="lax", max_age=12 * 3600)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    app.state.__dict__.update(cfg=cfg, store=store, lib=lib, dl=dl, hist=hist, funnel=funnel)

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
        return render(req, "index.html", guest=g, max_min=cfg.max_duration // 60)

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
        for t in results:
            cache.put(t)
        return render(req, "_results.html", results=results, max_dur=cfg.max_duration)

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
    def dj(req: Request, err: str = ""):
        if not is_dj(req):
            return render(req, "dj_login.html")
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
                      funnel=fun_on, code=code, hist=hist.status, xml=str(cfg.xml_path), err=err)

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
            store.reset_night()
            lib.refresh()
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
            except Exception as e:
                return RedirectResponse("/dj?err=" + quote(f"Funnel: {str(e)[:120]}"), 303)
        else:
            return Response(status_code=404)
        return back()

    return app
