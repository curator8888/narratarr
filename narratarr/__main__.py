import argparse
import logging
import os
from importlib.metadata import PackageNotFoundError, version

import uvicorn


def _version() -> str:
    try:
        return version("narratarr")
    except PackageNotFoundError:
        return "dev"


def main() -> None:
    parser = argparse.ArgumentParser(prog="narratarr", description="Run the Narratarr web app.")
    parser.add_argument(
        "--host",
        default=os.environ.get("NARRATARR_HOST", "127.0.0.1"),
        help="Address to listen on (default: 127.0.0.1, or $NARRATARR_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("NARRATARR_PORT", 8484)),
        help="Port to listen on (default: 8484, or $NARRATARR_PORT)",
    )
    parser.add_argument("--version", action="version", version=f"narratarr {_version()}")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and some services (SABnzbd, Prowlarr links) put API keys in URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"\n  Narratarr {_version()} is running → http://{shown}:{args.port}\n")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("NARRATARR_PASSWORD"):
        print("  Reachable from your network with no password. Anyone on it can change settings and\n"
              "  download to this machine. Set NARRATARR_PASSWORD if that isn't just you.\n")
    uvicorn.run("narratarr.api:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
