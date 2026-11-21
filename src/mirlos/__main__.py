import logging

import uvicorn

from .app import create_app
from .config import Config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = Config()
    print(f"\n  Mirlos  →  http://0.0.0.0:{cfg.port}/   DJ: /dj   PIN: {cfg.dj_pin}")
    print(f"  rekordbox.xml: {cfg.xml_path}")
    print(f"  downloads:     {cfg.requests_dir}\n")
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()
