"""Entry point: ``synopixtri`` or ``python -m synopixtri``."""

from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import load_bootstrap


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    boot = load_bootstrap()
    uvicorn.run(create_app(boot), host=boot.host, port=boot.port, log_level="info")


if __name__ == "__main__":
    main()
