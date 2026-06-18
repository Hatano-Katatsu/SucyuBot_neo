import argparse
import asyncio
import logging

from .service import TelegramComfyUIService


def main():
    parser = argparse.ArgumentParser(description="Telegram native Bot API service for ComfyUI selfies.")
    parser.add_argument("--config", default="data/config.json", help="Path to JSON config.")
    parser.add_argument("--state", default="data/state.json", help="Path to JSON state.")
    parser.add_argument("--log-level", default="INFO", help="Python logging level.")
    parser.add_argument("--web-host", default=None, help="Override Web console host.")
    parser.add_argument("--web-port", type=int, default=None, help="Override Web console port.")
    parser.add_argument("--no-web", action="store_true", help="Disable the Web console.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = TelegramComfyUIService(args.config, args.state)
    if args.web_host is not None:
        service.config["web_host"] = args.web_host
    if args.web_port is not None:
        service.config["web_port"] = args.web_port
    if args.no_web:
        service.config["web_enabled"] = False
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
