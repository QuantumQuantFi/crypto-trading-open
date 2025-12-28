import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..config.debug_config import DebugConfig
from ..core.orchestrator import ArbitrageOrchestrator


logger = logging.getLogger(__name__)

DEFAULT_BASELINE_SYMBOLS = ["BTC-USDC-PERP", "ETH-USDC-PERP"]
DEFAULT_WATCHLIST_TTL_SECONDS = 3600


@dataclass
class WatchItem:
    exchange: str
    symbol: str
    created_at: float
    updated_at: float
    expire_at: Optional[float]  # None 表示永久
    source: Optional[str] = None
    reason: Optional[str] = None

    @property
    def ttl_seconds(self) -> Optional[int]:
        if self.expire_at is None:
            return None
        return max(int(self.expire_at - time.time()), 0)


class SqliteWatchlistStore:
    """本地持久化 watchlist（简单、无额外依赖）。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    exchange TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expire_at REAL,
                    source TEXT,
                    reason TEXT,
                    PRIMARY KEY (exchange, symbol)
                );
                """
            )

    def load_active_items(self, *, now: float) -> Tuple[List[WatchItem], List[Tuple[str, str]]]:
        active: List[WatchItem] = []
        expired_keys: List[Tuple[str, str]] = []
        with self._connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT exchange, symbol, created_at, updated_at, expire_at, source, reason FROM watchlist"
                )
            )
        for r in rows:
            expire_at = r["expire_at"]
            if expire_at is not None and float(expire_at) <= now:
                expired_keys.append((str(r["exchange"]), str(r["symbol"])))
                continue
            active.append(
                WatchItem(
                    exchange=str(r["exchange"]),
                    symbol=str(r["symbol"]),
                    created_at=float(r["created_at"]),
                    updated_at=float(r["updated_at"]),
                    expire_at=None if expire_at is None else float(expire_at),
                    source=None if r["source"] is None else str(r["source"]),
                    reason=None if r["reason"] is None else str(r["reason"]),
                )
            )
        return active, expired_keys

    def upsert_items(self, items: List[WatchItem]) -> None:
        if not items:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO watchlist (exchange, symbol, created_at, updated_at, expire_at, source, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, symbol) DO UPDATE SET
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    expire_at=excluded.expire_at,
                    source=excluded.source,
                    reason=excluded.reason
                """,
                [
                    (
                        i.exchange,
                        i.symbol,
                        float(i.created_at),
                        float(i.updated_at),
                        None if i.expire_at is None else float(i.expire_at),
                        i.source,
                        i.reason,
                    )
                    for i in items
                ],
            )

    def delete_keys(self, keys: List[Tuple[str, str]]) -> None:
        if not keys:
            return
        with self._connect() as conn:
            conn.executemany("DELETE FROM watchlist WHERE exchange=? AND symbol=?", list(keys))


class WatchlistManager:
    """按 (exchange, symbol) 维护 TTL 的关注列表（用于动态订阅与入队过滤）"""

    def __init__(self, default_exchanges: Iterable[str], store: Optional[SqliteWatchlistStore] = None):
        self._default_exchanges = list(default_exchanges)
        self._items: Dict[Tuple[str, str], WatchItem] = {}
        self._lock = asyncio.Lock()
        self._store = store

    @staticmethod
    def _normalize_exchange(exchange: str) -> str:
        return (exchange or "").strip().lower()

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return (symbol or "").strip().upper()

    def is_active(self, exchange: str, symbol: str) -> bool:
        exchange = self._normalize_exchange(exchange)
        symbol = self._normalize_symbol(symbol)
        item = self._items.get((exchange, symbol))
        if not item:
            return False
        if item.expire_at is None:
            return True
        return item.expire_at > time.time()

    def active_pairs(self) -> List[Tuple[str, str]]:
        now = time.time()
        pairs: List[Tuple[str, str]] = []
        for (exchange, symbol), item in self._items.items():
            if item.expire_at is None or item.expire_at > now:
                pairs.append((exchange, symbol))
        return pairs

    def active_symbols(self) -> List[str]:
        now = time.time()
        symbols: Set[str] = set()
        for (_, symbol), item in self._items.items():
            if item.expire_at is None or item.expire_at > now:
                symbols.add(symbol)
        return list(symbols)

    async def load_from_store(self) -> None:
        if self._store is None:
            return
        now = time.time()
        items, expired_keys = await asyncio.to_thread(self._store.load_active_items, now=now)
        async with self._lock:
            for it in items:
                key = (self._normalize_exchange(it.exchange), self._normalize_symbol(it.symbol))
                self._items[key] = WatchItem(
                    exchange=key[0],
                    symbol=key[1],
                    created_at=float(it.created_at),
                    updated_at=float(it.updated_at),
                    expire_at=it.expire_at,
                    source=it.source,
                    reason=it.reason,
                )
        if expired_keys:
            # 清理已过期的持久化数据，避免不断膨胀
            await asyncio.to_thread(self._store.delete_keys, expired_keys)

    async def add(
        self,
        symbol: str,
        exchanges: Optional[List[str]] = None,
        ttl_seconds: Optional[int] = DEFAULT_WATCHLIST_TTL_SECONDS,
        source: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
        """返回 (new_pairs, touched_pairs)"""
        now = time.time()
        symbol = self._normalize_symbol(symbol)
        target_exchanges = [self._normalize_exchange(e) for e in (exchanges or self._default_exchanges)]
        new_pairs: List[Tuple[str, str]] = []
        touched_pairs: List[Tuple[str, str]] = []

        expire_at: Optional[float] = None if ttl_seconds is None else (now + int(ttl_seconds))

        async with self._lock:
            for exchange in target_exchanges:
                key = (exchange, symbol)
                existing = self._items.get(key)
                if existing is None:
                    self._items[key] = WatchItem(
                        exchange=exchange,
                        symbol=symbol,
                        created_at=now,
                        updated_at=now,
                        expire_at=expire_at,
                        source=source,
                        reason=reason,
                    )
                    new_pairs.append(key)
                else:
                    existing.updated_at = now
                    if existing.expire_at is None or expire_at is None:
                        existing.expire_at = None
                    else:
                        existing.expire_at = expire_at
                    touched_pairs.append(key)

        if self._store is not None:
            # 只写入受影响的 items
            async with self._lock:
                changed = [self._items[k] for k in (new_pairs + touched_pairs) if k in self._items]
            try:
                await asyncio.to_thread(self._store.upsert_items, changed)
            except Exception:
                logger.exception("watchlist store upsert failed")

        return new_pairs, touched_pairs

    async def touch(
        self,
        exchange: str,
        symbol: str,
        ttl_seconds: int = DEFAULT_WATCHLIST_TTL_SECONDS,
        source: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """存在则续命并返回 True，不存在则创建并返回 False（仍会变为 active）"""
        exchange = self._normalize_exchange(exchange)
        symbol = self._normalize_symbol(symbol)
        new_pairs, _ = await self.add(
            symbol=symbol,
            exchanges=[exchange],
            ttl_seconds=ttl_seconds,
            source=source,
            reason=reason,
        )
        return len(new_pairs) == 0

    async def remove(self, symbol: str, exchanges: Optional[List[str]] = None) -> List[Tuple[str, str]]:
        removed: List[Tuple[str, str]] = []
        symbol = self._normalize_symbol(symbol)
        target_exchanges = [self._normalize_exchange(e) for e in (exchanges or self._default_exchanges)]
        async with self._lock:
            for exchange in target_exchanges:
                key = (exchange, symbol)
                if key in self._items:
                    self._items.pop(key, None)
                    removed.append(key)
        if removed and self._store is not None:
            try:
                await asyncio.to_thread(self._store.delete_keys, removed)
            except Exception:
                logger.exception("watchlist store delete failed")
        return removed

    async def prune_expired(self) -> List[Tuple[str, str]]:
        now = time.time()
        removed: List[Tuple[str, str]] = []
        async with self._lock:
            for key, item in list(self._items.items()):
                if item.expire_at is not None and item.expire_at <= now:
                    self._items.pop(key, None)
                    removed.append(key)
        if removed and self._store is not None:
            try:
                await asyncio.to_thread(self._store.delete_keys, removed)
            except Exception:
                logger.exception("watchlist store delete failed")
        return removed

    async def reset_all_ttl(self, ttl_seconds: Optional[int]) -> int:
        now = time.time()
        expire_at: Optional[float] = None if ttl_seconds is None else (now + int(ttl_seconds))
        async with self._lock:
            for item in self._items.values():
                item.updated_at = now
                item.expire_at = expire_at
            changed = list(self._items.values())

        if self._store is not None:
            try:
                await asyncio.to_thread(self._store.upsert_items, changed)
            except Exception:
                logger.exception("watchlist store reset ttl failed")
        return len(changed)

    def snapshot(self) -> List[Dict[str, Any]]:
        now = time.time()
        out: List[Dict[str, Any]] = []
        for item in self._items.values():
            active = item.expire_at is None or item.expire_at > now
            out.append(
                {
                    "exchange": item.exchange,
                    "symbol": item.symbol,
                    "active": active,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "expire_at": item.expire_at,
                    "ttl_seconds": item.ttl_seconds,
                    "source": item.source,
                    "reason": item.reason,
                }
            )
        out.sort(key=lambda x: (x["symbol"], x["exchange"]))
        return out


class SubscriptionController:
    """统一处理动态 subscribe/unsubscribe（尽可能复用现有适配器能力）"""

    def __init__(self, orchestrator: ArbitrageOrchestrator, max_concurrent: int = 10):
        self._orchestrator = orchestrator
        self._sem = asyncio.Semaphore(max_concurrent)
        self._subscribed: Set[Tuple[str, str]] = set()  # (exchange, std_symbol)

    @staticmethod
    def _read_orderbook_depth() -> Optional[int]:
        raw = os.getenv("MONITOR_ORDERBOOK_DEPTH")
        if not raw:
            return None
        try:
            depth = int(str(raw).strip())
        except Exception:
            return None
        return depth if depth > 0 else None

    async def subscribe_pair(self, exchange: str, std_symbol: str) -> None:
        key = (exchange, std_symbol)
        if key in self._subscribed:
            return

        adapter = self._orchestrator.data_receiver.adapters.get(exchange)
        if adapter is None:
            return

        async with self._sem:
            try:
                exchange_symbol = self._orchestrator.data_receiver.symbol_converter.convert_to_exchange(std_symbol, exchange)
            except Exception:
                return
            ob_cb = self._orchestrator.data_receiver._create_orderbook_callback(exchange)
            tk_cb = self._orchestrator.data_receiver._create_ticker_callback(exchange)

            if hasattr(adapter, "subscribe_orderbook"):
                depth = self._read_orderbook_depth()
                if depth is None:
                    await adapter.subscribe_orderbook(exchange_symbol, callback=ob_cb)
                else:
                    try:
                        await adapter.subscribe_orderbook(exchange_symbol, callback=ob_cb, depth=depth)
                    except TypeError:
                        await adapter.subscribe_orderbook(exchange_symbol, callback=ob_cb)
            if hasattr(adapter, "subscribe_ticker"):
                await adapter.subscribe_ticker(exchange_symbol, callback=tk_cb)

            self._subscribed.add(key)

    async def unsubscribe_pair(self, exchange: str, std_symbol: str) -> None:
        key = (exchange, std_symbol)
        adapter = self._orchestrator.data_receiver.adapters.get(exchange)
        if adapter is None:
            self._subscribed.discard(key)
            return

        async with self._sem:
            try:
                exchange_symbol = self._orchestrator.data_receiver.symbol_converter.convert_to_exchange(std_symbol, exchange)
            except Exception:
                self._subscribed.discard(key)
                return
            if hasattr(adapter, "unsubscribe_orderbook"):
                await adapter.unsubscribe_orderbook(exchange_symbol)
            if hasattr(adapter, "unsubscribe_ticker"):
                await adapter.unsubscribe_ticker(exchange_symbol)

        self._subscribed.discard(key)


class MonitorApiRuntime:
    """Headless orchestrator + watchlist + 动态订阅控制（供 FastAPI 调用）"""

    def __init__(self, config_path: Path, debug_config: Optional[DebugConfig] = None, enable_ui: bool = False):
        self.orchestrator = ArbitrageOrchestrator(config_path, debug_config or DebugConfig(), enable_ui=enable_ui)
        repo_root = self._guess_repo_root(config_path)
        store = SqliteWatchlistStore(repo_root / "data" / "monitor_v2_watchlist.sqlite3")
        self.watchlist = WatchlistManager(default_exchanges=self.orchestrator.config.exchanges, store=store)
        self.subscriptions = SubscriptionController(self.orchestrator)
        self._prune_task: Optional[asyncio.Task] = None
        self._symbol_order: List[str] = list(DEFAULT_BASELINE_SYMBOLS)
        self.started: bool = False
        self.starting: bool = False
        self.start_error: Optional[str] = None
        self.started_at: Optional[float] = None

    @staticmethod
    def _guess_repo_root(config_path: Path) -> Path:
        try:
            resolved = config_path.resolve()
            # 常见路径：<repo>/config/arbitrage/xxx.yaml
            for parent in resolved.parents:
                if parent.name == "config":
                    return parent.parent
            if (resolved.parent / "config").exists():
                return resolved.parent
        except Exception:
            pass
        return Path.cwd()

    async def start(self) -> None:
        if self.started or self.starting:
            return
        self.starting = True
        self.start_error = None
        # 过滤：只允许 watchlist 中 active 的 (exchange, symbol) 入队
        self.orchestrator.data_receiver.set_should_accept(self.watchlist.is_active)

        # 1) 先恢复本地持久化的 watchlist（只恢复未过期的条目）
        await self.watchlist.load_from_store()

        # 2) 默认只保留 BTC/ETH 两个币种作为初始 baseline（永久关注）
        for symbol in DEFAULT_BASELINE_SYMBOLS:
            await self.watchlist.add(symbol=symbol, ttl_seconds=None, source="baseline", reason="default")

        # 3) 避免按配置文件的 symbols 做全量订阅：先清空 config.symbols，再由 watchlist 动态订阅
        await self.orchestrator.set_symbols([])

        try:
            await self.orchestrator.start()
            # 启动后按 watchlist 对 (exchange, symbol) 做动态订阅
            for exchange, symbol in self.watchlist.active_pairs():
                try:
                    await self.subscriptions.subscribe_pair(exchange, symbol)
                except Exception:
                    pass
            await self._refresh_symbols_for_analysis()
            self._prune_task = asyncio.create_task(self._prune_loop())
            self.started = True
            self.started_at = time.time()
        except Exception as e:
            self.start_error = str(e)
            raise
        finally:
            self.starting = False

    async def stop(self) -> None:
        self.starting = False
        if self._prune_task:
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass
        await self.orchestrator.stop()
        self.started = False

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(10)
            expired = await self.watchlist.prune_expired()
            if expired:
                for exchange, symbol in expired:
                    try:
                        await self.subscriptions.unsubscribe_pair(exchange, symbol)
                    except Exception:
                        pass
                await self._refresh_symbols_for_analysis()

    async def _refresh_symbols_for_analysis(self) -> None:
        active = set(self.watchlist.active_symbols())
        ordered: List[str] = [s for s in self._symbol_order if s in active]
        for s in active:
            if s not in ordered:
                ordered.append(s)
        await self.orchestrator.set_symbols(ordered)

    async def add_watch(
        self,
        symbol: str,
        exchanges: Optional[List[str]] = None,
        ttl_seconds: int = DEFAULT_WATCHLIST_TTL_SECONDS,
        source: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        symbol = self.watchlist._normalize_symbol(symbol)
        if exchanges:
            exchanges = [self.watchlist._normalize_exchange(e) for e in exchanges]
        new_pairs, touched_pairs = await self.watchlist.add(
            symbol=symbol,
            exchanges=exchanges,
            ttl_seconds=ttl_seconds,
            source=source,
            reason=reason,
        )

        if symbol not in self._symbol_order:
            self._symbol_order.append(symbol)

        # 新增的 pair 立即尝试订阅（注意：即使交易所不支持 unsubscribe，也至少能通过过滤停止入队）
        for exchange, sym in new_pairs:
            try:
                await self.subscriptions.subscribe_pair(exchange, sym)
            except Exception:
                pass

        await self._refresh_symbols_for_analysis()
        return {
            "symbol": symbol,
            "new_pairs": [{"exchange": e, "symbol": s} for e, s in new_pairs],
            "touched_pairs": [{"exchange": e, "symbol": s} for e, s in touched_pairs],
        }

    async def touch_watch(
        self,
        exchange: str,
        symbol: str,
        ttl_seconds: int = DEFAULT_WATCHLIST_TTL_SECONDS,
        source: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        exchange = self.watchlist._normalize_exchange(exchange)
        symbol = self.watchlist._normalize_symbol(symbol)
        existed = await self.watchlist.touch(
            exchange=exchange,
            symbol=symbol,
            ttl_seconds=ttl_seconds,
            source=source,
            reason=reason,
        )
        if symbol not in self._symbol_order:
            self._symbol_order.append(symbol)

        try:
            await self.subscriptions.subscribe_pair(exchange, symbol)
        except Exception:
            pass

        await self._refresh_symbols_for_analysis()
        return {"exchange": exchange, "symbol": symbol, "extended": existed}

    async def remove_watch(self, symbol: str, exchanges: Optional[List[str]] = None) -> Dict[str, Any]:
        symbol = self.watchlist._normalize_symbol(symbol)
        if exchanges:
            exchanges = [self.watchlist._normalize_exchange(e) for e in exchanges]
        removed = await self.watchlist.remove(symbol=symbol, exchanges=exchanges)
        for exchange, sym in removed:
            try:
                await self.subscriptions.unsubscribe_pair(exchange, sym)
            except Exception:
                pass
        await self._refresh_symbols_for_analysis()
        return {"symbol": symbol, "removed_pairs": [{"exchange": e, "symbol": s} for e, s in removed]}

    async def reset_watchlist_ttl(self, ttl_seconds: Optional[int]) -> Dict[str, Any]:
        count = await self.watchlist.reset_all_ttl(ttl_seconds)
        await self._refresh_symbols_for_analysis()
        return {"updated": count, "ttl_seconds": ttl_seconds}

    async def health(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "generated_at": time.time(),
            "starting": self.starting,
            "started": self.started,
            "started_at": self.started_at,
            "start_error": self.start_error,
            "watchlist_pairs": len(self.watchlist.active_pairs()),
        }
        if not self.started:
            return payload

        stats = self.orchestrator.get_stats()
        analysis = await self.orchestrator.get_latest_analysis()
        payload["stats"] = stats
        payload["analysis"] = {
            "last_analysis_at": analysis.get("last_analysis_at"),
            "opportunities_count": len(analysis.get("opportunities") or []),
            "symbols_with_spreads": len(analysis.get("symbol_spreads") or {}),
        }
        return payload
