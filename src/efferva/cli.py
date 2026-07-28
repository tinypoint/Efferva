import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="efferva")
    parser.add_argument(
        "application",
        nargs="?",
        default="efferva.example:app",
        help="ASGI application import string (default: bundled development example)",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        args.application,
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=False,
    )
