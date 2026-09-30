"""Start the API on this laptop only: `python -m backend.serve [--reload] [--port 8000]`.

The address is always 127.0.0.1 and cannot be changed here, so the API is never reachable
from another device on the network (PIE has no login).
"""

import argparse

import uvicorn

from backend.core.local_only import LOCAL_HOST


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the PIE API on 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    args = parser.parse_args(argv)
    uvicorn.run("backend.main:app", host=LOCAL_HOST, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
