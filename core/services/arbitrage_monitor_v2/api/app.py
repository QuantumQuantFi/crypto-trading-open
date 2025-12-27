from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
import time
import asyncio
import json
import heapq

from .runtime import MonitorApiRuntime, DEFAULT_WATCHLIST_TTL_SECONDS
from .web_ui import render_monitor_ui_html


class WatchAddRequest(BaseModel):
    symbol: str = Field(..., description="标准符号，例如 BTC-USDC-PERP")
    exchanges: Optional[List[str]] = Field(default=None, description="为空表示所有已配置交易所")
    ttl_seconds: int = Field(default=DEFAULT_WATCHLIST_TTL_SECONDS, ge=60, description="默认关注 1h")
    source: Optional[str] = None
    reason: Optional[str] = None


class WatchTouchRequest(BaseModel):
    exchange: str
    symbol: str
    ttl_seconds: int = Field(default=DEFAULT_WATCHLIST_TTL_SECONDS, ge=60)
    source: Optional[str] = None
    reason: Optional[str] = None


class WatchRemoveRequest(BaseModel):
    symbol: str
    exchanges: Optional[List[str]] = None


def _safe_float(v: object, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)  # type: ignore[arg-type]
    except Exception:
        return default


def create_app(config_path: Path, enable_ui: bool = False) -> FastAPI:
    app = FastAPI(title="Monitor/V2 Data Service", version="0.1.0")
    runtime = MonitorApiRuntime(config_path=config_path, enable_ui=enable_ui)
    app.state.runtime = runtime

    @app.on_event("startup")
    async def _startup():
        # 不阻塞服务启动：在事件循环空闲后再启动 runtime（避免影响 Uvicorn 生命周期启动）
        app.state._runtime_start_task = None

        def _kickoff() -> None:
            task = asyncio.create_task(runtime.start())
            app.state._runtime_start_task = task

            def _consume_task_result(t: asyncio.Task) -> None:
                try:
                    _ = t.result()
                except Exception:
                    pass

            task.add_done_callback(_consume_task_result)

        loop = asyncio.get_running_loop()
        loop.call_later(0.1, _kickoff)

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

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/ui")

    @app.get("/ui")
    async def ui() -> HTMLResponse:
        return HTMLResponse(render_monitor_ui_html())

    async def _build_dashboard_snapshot(
        *,
        interval_ms: int,
        top_spreads: int,
        top_opps: int,
        symbol_like: Optional[str],
        min_abs_spread_pct: Optional[float],
    ) -> Dict[str, Any]:
        interval_ms = max(int(interval_ms or 1000), 200)
        top_spreads = max(min(int(top_spreads or 200), 2000), 0)
        top_opps = max(min(int(top_opps or 50), 500), 0)
        symbol_like_norm = (symbol_like or "").strip().upper()
        min_abs = None if min_abs_spread_pct is None else float(min_abs_spread_pct)

        now = time.time()
        watch_pairs = runtime.watchlist.active_pairs()
        watchlist_exchanges = sorted({e for e, _ in watch_pairs})
        watchlist_symbols = sorted({s for _, s in watch_pairs})
        if not runtime.started:
            return {
                "type": "snapshot",
                "generated_at": now,
                "started": runtime.started,
                "starting": runtime.starting,
                "start_error": runtime.start_error,
                "watchlist_pairs": len(watch_pairs),
                "watchlist_exchanges": watchlist_exchanges,
                "watchlist_symbols": watchlist_symbols,
                "analysis_age_ms": None,
                "last_analysis_at": None,
                "symbols_with_spreads": 0,
                "opportunities_count": 0,
                "top_spreads": top_spreads,
                "top_opps": top_opps,
                "opportunities": [],
                "top_spread_rows": [],
                "queue_info": "starting",
            }

        analysis = await runtime.orchestrator.get_latest_analysis()
        last_at = analysis.get("last_analysis_at")
        analysis_age_ms = None if not last_at else max((now - float(last_at)) * 1000, 0.0)

        opps: List[Dict[str, Any]] = list(analysis.get("opportunities") or [])
        if symbol_like_norm:
            opps = [o for o in opps if symbol_like_norm in str(o.get("symbol", "")).upper()]
        opps.sort(key=lambda o: abs(_safe_float(o.get("spread_pct"), 0.0)), reverse=True)
        if top_opps:
            opps = opps[:top_opps]
        else:
            opps = []

        symbol_spreads: Dict[str, List[Dict[str, Any]]] = dict(analysis.get("symbol_spreads") or {})

        def _iter_spreads():
            for sym, rows in symbol_spreads.items():
                if symbol_like_norm and symbol_like_norm not in sym.upper():
                    continue
                for r in rows or []:
                    pct = _safe_float(r.get("spread_pct"), 0.0)
                    if min_abs is not None and abs(pct) < min_abs:
                        continue
                    yield r

        top_rows: List[Dict[str, Any]] = []
        if top_spreads > 0:
            top_rows = heapq.nlargest(
                top_spreads,
                _iter_spreads(),
                key=lambda r: abs(_safe_float(r.get("spread_pct"), 0.0)),
            )

        stats = runtime.orchestrator.get_stats()
        proc_stats = stats.get("data_processor") or {}
        queue_info = (
            f"ob_q={proc_stats.get('orderbook_queue_size', '-')}"
            f"(peak={proc_stats.get('orderbook_queue_peak', '-')}) "
            f"tk_q={proc_stats.get('ticker_queue_size', '-')}"
            f"(peak={proc_stats.get('ticker_queue_peak', '-')}) "
            f"ob_p95={_safe_float(proc_stats.get('orderbook_delay_p95_ms'), 0.0):.1f}ms "
            f"tk_p95={_safe_float(proc_stats.get('ticker_delay_p95_ms'), 0.0):.1f}ms"
        )

        return {
            "type": "snapshot",
            "generated_at": now,
            "started": runtime.started,
            "starting": runtime.starting,
            "start_error": runtime.start_error,
            "watchlist_pairs": len(watch_pairs),
            "watchlist_exchanges": watchlist_exchanges,
            "watchlist_symbols": watchlist_symbols,
            "analysis_age_ms": analysis_age_ms,
            "last_analysis_at": last_at,
            "symbols_with_spreads": len(symbol_spreads),
            "opportunities_count": len(analysis.get("opportunities") or []),
            "top_spreads": top_spreads,
            "top_opps": top_opps,
            "opportunities": opps,
            "top_spread_rows": top_rows,
            "queue_info": queue_info,
        }

    @app.get("/ui/data")
    async def ui_data(
        interval_ms: int = Query(default=1000),
        top_spreads: int = Query(default=200),
        top_opps: int = Query(default=50),
        symbol_like: Optional[str] = Query(default=None),
        min_abs_spread_pct: Optional[float] = Query(default=None),
    ) -> Dict[str, Any]:
        return await _build_dashboard_snapshot(
            interval_ms=interval_ms,
            top_spreads=top_spreads,
            top_opps=top_opps,
            symbol_like=symbol_like,
            min_abs_spread_pct=min_abs_spread_pct,
        )

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

    @app.websocket("/ws/stream")
    async def ws_stream(
        websocket: WebSocket,
        interval_ms: int = 1000,
        top_spreads: int = 200,
        top_opps: int = 50,
        symbol_like: Optional[str] = None,
        min_abs_spread_pct: Optional[float] = None,
    ) -> None:
        await websocket.accept()
        try:
            while True:
                payload = await _build_dashboard_snapshot(
                    interval_ms=interval_ms,
                    top_spreads=top_spreads,
                    top_opps=top_opps,
                    symbol_like=symbol_like,
                    min_abs_spread_pct=min_abs_spread_pct,
                )
                await websocket.send_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                await asyncio.sleep(interval_ms / 1000)
        except WebSocketDisconnect:
            return
        except Exception:
            try:
                await websocket.close()
            except Exception:
                pass

    return app
