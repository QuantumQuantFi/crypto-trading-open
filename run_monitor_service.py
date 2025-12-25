import argparse
from pathlib import Path

import uvicorn

from core.services.arbitrage_monitor_v2.api.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor/V2 Headless Data Service (FastAPI)")
    parser.add_argument(
        "--config",
        default="config/arbitrage/monitor_v2_ws_only_45.yaml",
        help="Monitor/V2 配置文件路径",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = create_app(Path(args.config))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

