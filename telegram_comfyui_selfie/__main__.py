import argparse
import asyncio
import logging
from pathlib import Path

from .service import TelegramComfyUIService


def main():
    parser = argparse.ArgumentParser(
        description="SucyuBot — Telegram roleplay image bot with ComfyUI/AnimaTool backend.",
        epilog="Quick start: py -3 -m telegram_comfyui_selfie",
    )
    parser.add_argument(
        "--config", default=None,
        help="Path to config file (.yml or .json). Defaults to data/config.yml, falls back to data/config.json.",
    )
    parser.add_argument("--state", default="data/state.json", help="Path to JSON state file.")
    parser.add_argument("--log-level", default="INFO", help="Python logging level.")
    parser.add_argument("--web-host", default=None, help="Override Web console host.")
    parser.add_argument("--web-port", type=int, default=None, help="Override Web console port.")
    parser.add_argument("--no-web", action="store_true", help="Disable the Web console.")
    args = parser.parse_args()

    # config 默认逻辑：优先 data/config.yml，不存在则回退 data/config.json
    config = args.config
    if config is None:
        yml = Path("data/config.yml")
        json = Path("data/config.json")
        config = str(yml) if yml.exists() else str(json)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = TelegramComfyUIService(config, args.state)
    if args.web_host is not None:
        service.config["web_host"] = args.web_host
    if args.web_port is not None:
        service.config["web_port"] = args.web_port
    if args.no_web:
        service.config["web_enabled"] = False
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
