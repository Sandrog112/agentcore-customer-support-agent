"""Start the test console: uv run --env-file .env python -m web [--port 8765]"""

import argparse
import logging

import uvicorn

from web.server import HOST, PORT, app

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--port", type=int, default=PORT)
args = parser.parse_args()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
print(f"Support agent console: http://{HOST}:{args.port}", flush=True)
uvicorn.run(app, host=HOST, port=args.port, log_level="warning")
