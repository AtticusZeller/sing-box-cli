"""Private entry point spawned by config serve start."""

import argparse
import logging
import signal
import threading
from pathlib import Path

import psutil

from .serve import ProcessState, process_state
from .serve_backend import ConfigHTTPServer, GitHub, github_token, write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    github = GitHub(github_token())
    try:
        with ConfigHTTPServer((args.host, args.port), args.state_dir, github) as server:

            def shutdown(_signal: int, _frame: object) -> None:
                threading.Thread(target=server.shutdown, daemon=True).start()

            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
            process = psutil.Process()
            state = ProcessState(
                pid=process.pid,
                created_at=process.create_time(),
                instance=args.instance,
                host=args.host,
                port=args.port,
            )
            write_json(args.state_dir / "process.json", state.model_dump())
            logging.info(
                "Configuration service listening on %s:%s", args.host, args.port
            )
            server.serve_forever(poll_interval=0.1)
            logging.info("Configuration service stopped")
    finally:
        github.close()
        current = process_state(args.state_dir)
        if current is not None and current.instance == args.instance:
            (args.state_dir / "process.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
