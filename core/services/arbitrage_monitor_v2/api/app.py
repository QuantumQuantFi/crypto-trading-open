from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
import time
import asyncio

from .runtime import MonitorApiRuntime


class WatchAddRequest(BaseModel):
    symbol: str = Field(..., description="标准符号，例如 BTC-USDC-PERP")
    exchanges: Optional[List[str]] = Field(default=None, description="为空表示所有已配置交易所")
    ttl_seconds: int = Field(default=86400, ge=60, description="默认关注 24h")
    source: Optional[str] = None
    reason: Optional[str] = None


class WatchTouchRequest(BaseModel):
    exchange: str
    symbol: str
    ttl_seconds: int = Field(default=86400, ge=60)
    source: Optional[str] = None
    reason: Optional[str] = None


class WatchRemoveRequest(BaseModel):
    symbol: str
    exchanges: Optional[List[str]] = None


def create_app(config_path: Path) -> FastAPI:
    app = FastAPI(title="Monitor/V2 Data Service", version="0.1.0")
    runtime = MonitorApiRuntime(config_path=config_path)
    app.state.runtime = runtime

    @app.on_event("startup")
    async def _startup():
        # 不阻塞服务启动：后台连接交易所并订阅
        task = asyncio.create_task(runtime.start())
        app.state._runtime_start_task = task

        def _consume_task_result(t: asyncio.Task) -> None:
            try:
                _ = t.result()
            except Exception:
                pass

        task.add_done_callback(_consume_task_result)

    @app.on_event("shutdown")
    async def _shutdown():
        task = getattr(app.state, "_runtime_start_task", None)
        if task:
            task.cancel()
            try:
                await task
            except Exception:
                pass
        await runtime.stop()

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return await runtime.health()

    @app.get("/watchlist")
    async def get_watchlist() -> Dict[str, Any]:
        return {"items": runtime.watchlist.snapshot()}

    @app.post("/watchlist/add")
    async def add_watch(req: WatchAddRequest) -> Dict[str, Any]:
        return await runtime.add_watch(
            symbol=req.symbol,
            exchanges=req.exchanges,
            ttl_seconds=req.ttl_seconds,
            source=req.source,
            reason=req.reason,
        )

    @app.post("/watchlist/touch")
    async def touch_watch(req: WatchTouchRequest) -> Dict[str, Any]:
        return await runtime.touch_watch(
            exchange=req.exchange,
            symbol=req.symbol,
            ttl_seconds=req.ttl_seconds,
            source=req.source,
            reason=req.reason,
        )

    @app.post("/watchlist/remove")
    async def remove_watch(req: WatchRemoveRequest) -> Dict[str, Any]:
        return await runtime.remove_watch(symbol=req.symbol, exchanges=req.exchanges)

    @app.get("/snapshot")
    async def snapshot(
        exchanges: Optional[List[str]] = Query(default=None),
        symbols: Optional[List[str]] = Query(default=None),
        include_tickers: bool = Query(default=False),
        include_analysis: bool = Query(default=True),
    ) -> Dict[str, Any]:
        pairs = runtime.watchlist.active_pairs()
        if exchanges:
            ex_set = {x.strip().lower() for x in exchanges}
            pairs = [(e, s) for (e, s) in pairs if e in ex_set]
        if symbols:
            sym_set = {x.strip().upper() for x in symbols}
            pairs = [(e, s) for (e, s) in pairs if s in sym_set]

        orderbooks: Dict[str, Dict[str, Any]] = {}
        tickers: Dict[str, Dict[str, Any]] = {}

        def _ts(dt) -> Optional[float]:
            if dt is None:
                return None
            try:
                return float(dt.timestamp())
            except Exception:
                return None

        for exchange, symbol in pairs:
            ob = runtime.orchestrator.data_processor.get_orderbook(exchange, symbol)
            if ob and ob.best_bid and ob.best_ask:
                orderbooks.setdefault(exchange, {})[symbol] = {
                    "bid_price": float(ob.best_bid.price),
                    "bid_size": float(ob.best_bid.size),
                    "ask_price": float(ob.best_ask.price),
                    "ask_size": float(ob.best_ask.size),
                    "exchange_timestamp": _ts(getattr(ob, "exchange_timestamp", None)),
                    "received_timestamp": _ts(getattr(ob, "received_timestamp", None)),
                    "processed_timestamp": _ts(getattr(ob, "processed_timestamp", None)),
                }

            if include_tickers:
                tk = runtime.orchestrator.data_processor.get_ticker(exchange, symbol)
                if tk:
                    tickers.setdefault(exchange, {})[symbol] = {
                        "timestamp": tk.timestamp.timestamp() if getattr(tk, "timestamp", None) else None,
                        "funding_rate": float(getattr(tk, "funding_rate", 0.0)) if getattr(tk, "funding_rate", None) is not None else None,
                        "last": float(getattr(tk, "last", 0.0)) if getattr(tk, "last", None) is not None else None,
                    }

        payload: Dict[str, Any] = {
            "generated_at": time.time(),
            "orderbooks": orderbooks,
        }
        if include_tickers:
            payload["tickers"] = tickers
        if include_analysis:
            payload["analysis"] = await runtime.orchestrator.get_latest_analysis()
        return payload

    return app
