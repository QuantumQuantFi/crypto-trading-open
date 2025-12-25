#!/usr/bin/env python3
"""
行情流性能基准测试（不启动 UI / 不做套利分析）

目标：
- 关注 N 个币种、M 个交易所时，验证数据接收/入队/出队处理是否出现明显积压
- 输出队列长度、丢包数量、以及“本地接收 -> 处理完成”的延迟统计（avg/p95/max）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config",
        default="config/arbitrage/monitor_v2_all_10.yaml",
        help="监控配置文件路径（monitor_v2*.yaml）",
    )
    p.add_argument("--duration", type=float, default=30.0, help="运行时长（秒）")
    p.add_argument("--warmup", type=float, default=5.0, help="预热时长（秒，不计入统计）")
    p.add_argument("--interval", type=float, default=5.0, help="统计输出间隔（秒）")
    return p.parse_args()


def _load_exchange_config(exchange: str, config_path: Path):
    from core.adapters.exchanges.interface import ExchangeConfig
    from core.adapters.exchanges.models import ExchangeType
    from core.utils.config_loader import ExchangeConfigLoader

    type_map = {
        "edgex": ExchangeType.PERPETUAL,
        "lighter": ExchangeType.PERPETUAL,
        "hyperliquid": ExchangeType.PERPETUAL,
        "binance": ExchangeType.PERPETUAL,
        "backpack": ExchangeType.PERPETUAL,
        "paradex": ExchangeType.PERPETUAL,
        "grvt": ExchangeType.PERPETUAL,
        "okx": ExchangeType.PERPETUAL,
        "variational": ExchangeType.PERPETUAL,
    }

    config_loader = ExchangeConfigLoader()
    auth = config_loader.load_auth_config(exchange, use_env=True, config_file=str(config_path))

    with config_path.open("r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}
    if exchange in config_data:
        config_data = config_data[exchange]

    api_key = auth.api_key or config_data.get("api_key", "")
    api_secret = (auth.api_secret or auth.private_key or config_data.get("api_secret", ""))

    extra_params = dict(config_data.get("extra_params", {}) or {})
    if auth.jwt_token:
        extra_params["jwt_token"] = auth.jwt_token
    if auth.l2_address:
        extra_params["l2_address"] = auth.l2_address
    if auth.sub_account_id:
        extra_params["sub_account_id"] = auth.sub_account_id
    if auth.wallet_address:
        extra_params.setdefault("wallet_address", auth.wallet_address)

    return ExchangeConfig(
        exchange_id=exchange,
        name=config_data.get("name", exchange),
        exchange_type=type_map.get(exchange, ExchangeType.PERPETUAL),
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=config_data.get("api_passphrase") or auth.api_passphrase,
        private_key=auth.private_key,
        wallet_address=auth.wallet_address,
        testnet=bool(config_data.get("testnet", False)),
        base_url=config_data.get("base_url"),
        ws_url=config_data.get("ws_url"),
        extra_params=extra_params,
    )


async def main() -> int:
    _load_env()
    args = _parse_args()

    # 让脚本可从任意目录执行
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    from core.adapters.exchanges.factory import ExchangeFactory
    from core.services.arbitrage_monitor_v2.config.monitor_config import ConfigManager
    from core.services.arbitrage_monitor_v2.config.debug_config import DebugConfig
    from core.services.arbitrage_monitor_v2.data.data_receiver import DataReceiver
    from core.services.arbitrage_monitor_v2.data.data_processor import DataProcessor

    config_path = (repo_root / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    cfg_mgr = ConfigManager(config_path)
    monitor_cfg = cfg_mgr.get_config()
    debug_cfg = DebugConfig.create_production()

    orderbook_queue = asyncio.Queue(maxsize=monitor_cfg.orderbook_queue_size)
    ticker_queue = asyncio.Queue(maxsize=monitor_cfg.ticker_queue_size)
    receiver = DataReceiver(orderbook_queue, ticker_queue, debug_cfg)
    processor = DataProcessor(orderbook_queue, ticker_queue, debug_cfg, scroller=None)

    factory = ExchangeFactory()

    # 1) 创建/连接适配器（尽量多连，失败不阻断）
    adapters = {}
    for ex in monitor_cfg.exchanges:
        ex_cfg_path = repo_root / f"config/exchanges/{ex}_config.yaml"
        if not ex_cfg_path.exists():
            print(f"⚠️  [{ex}] 缺少配置文件: {ex_cfg_path}（跳过）")
            continue
        try:
            ex_cfg = _load_exchange_config(ex, ex_cfg_path)
            adapters[ex] = factory.create_adapter(exchange_id=ex, config=ex_cfg)
        except Exception as e:
            print(f"❌ [{ex}] 创建适配器失败: {e}")

    async def _connect_one(name: str, adapter):
        try:
            ok = await adapter.connect()
            if ok is False:
                raise RuntimeError("connect() returned False")
            receiver.register_adapter(name, adapter)
            return name, True, None
        except Exception as e:
            return name, False, e

    results = await asyncio.gather(*[_connect_one(n, a) for n, a in adapters.items()])
    connected = [n for (n, ok, _) in results if ok]
    failed = [(n, err) for (n, ok, err) in results if not ok]

    print(f"✅ 已连接交易所: {connected}")
    if failed:
        print("⚠️  连接失败交易所:")
        for n, err in failed:
            print(f"  - {n}: {err}")

    if len(connected) < 1:
        print("❌ 没有任何交易所连接成功，结束")
        return 2

    # 2) 订阅行情
    await receiver.subscribe_all(monitor_cfg.symbols)
    await processor.start()

    # 3) 运行并输出统计
    start = time.time()
    warmed = False
    next_print = start
    last_print_at = start
    last = {
        "ob_recv": 0,
        "ob_drop": 0,
        "tk_recv": 0,
        "tk_drop": 0,
        "ob_proc": 0,
        "tk_proc": 0,
    }

    try:
        while True:
            now = time.time()

            # 预热：清掉启动阶段的积压与异常长尾样本
            if not warmed and (now - start) >= args.warmup:
                try:
                    processor._orderbook_delay_ms.clear()
                    processor._ticker_delay_ms.clear()
                except Exception:
                    pass
                rs = receiver.get_stats()
                ps = processor.get_stats()
                last = {
                    "ob_recv": int(rs.get("orderbook_received", 0)),
                    "ob_drop": int(rs.get("orderbook_dropped", 0)),
                    "tk_recv": int(rs.get("ticker_received", 0)),
                    "tk_drop": int(rs.get("ticker_dropped", 0)),
                    "ob_proc": int(ps.get("orderbook_processed", 0)),
                    "tk_proc": int(ps.get("ticker_processed", 0)),
                }
                start = now
                last_print_at = now
                next_print = now
                warmed = True
                print(f"✅ 预热完成，开始统计（duration={args.duration}s）")

            if warmed and (now - start) >= args.duration:
                break
            if now >= next_print:
                rs = receiver.get_stats()
                ps = processor.get_stats()

                ob_recv = int(rs.get("orderbook_received", 0))
                ob_drop = int(rs.get("orderbook_dropped", 0))
                tk_recv = int(rs.get("ticker_received", 0))
                tk_drop = int(rs.get("ticker_dropped", 0))
                ob_proc = int(ps.get("orderbook_processed", 0))
                tk_proc = int(ps.get("ticker_processed", 0))

                dt = max(1e-9, now - last_print_at)
                ob_rps = (ob_recv - last["ob_recv"]) / dt if dt >= 0.5 else 0.0
                tk_rps = (tk_recv - last["tk_recv"]) / dt if dt >= 0.5 else 0.0

                print(
                    f"[{now - start:6.1f}s] "
                    f"Q(ob/tk)={ps.get('orderbook_queue_size')}/{ps.get('ticker_queue_size')} "
                    f"drop(ob/tk)={ob_drop}/{tk_drop} "
                    f"rps(ob/tk)={ob_rps:.0f}/{tk_rps:.0f} "
                    f"delay_ms(ob avg/p95/max)={ps.get('orderbook_delay_avg_ms'):.1f}/"
                    f"{ps.get('orderbook_delay_p95_ms'):.1f}/{ps.get('orderbook_delay_max_ms'):.1f} "
                    f"delay_ms(tk avg/p95/max)={ps.get('ticker_delay_avg_ms'):.1f}/"
                    f"{ps.get('ticker_delay_p95_ms'):.1f}/{ps.get('ticker_delay_max_ms'):.1f}"
                )

                last = {
                    "ob_recv": ob_recv,
                    "ob_drop": ob_drop,
                    "tk_recv": tk_recv,
                    "tk_drop": tk_drop,
                    "ob_proc": ob_proc,
                    "tk_proc": tk_proc,
                }
                last_print_at = now
                next_print = now + args.interval

            await asyncio.sleep(0.2)
    finally:
        await processor.stop()
        await receiver.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
