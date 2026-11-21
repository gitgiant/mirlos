import logging

import uvicorn
import ytfetch

from .app import create_app
from .config import Config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = Config()
    signin = "off"
    if cfg.cookies_from_browser:
        try:
            n = ytfetch.use_browser_cookies(cfg.cookies_from_browser)
            if n:
                signin = f"{cfg.cookies_from_browser} ({n} YouTube cookies)"
            else:
                signin = f"{cfg.cookies_from_browser}, but not signed in to YouTube there"
        except ytfetch.FetchError as e:
            signin = f"off ({e})"
    print(f"\n  Mirlos  →  http://0.0.0.0:{cfg.port}/   DJ: /dj   PIN: {cfg.dj_pin}")
    print(f"  rekordbox.xml: {cfg.xml_path}")
    print(f"  downloads:     {cfg.requests_dir}")
    print(f"  YouTube login: {signin}\n", flush=True)
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
