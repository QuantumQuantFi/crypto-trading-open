# 提交说明（中文）

## Commit 标题（建议）
`monitor(v2): 默认仅 BTC/ETH + watchlist SQLite 持久化 + UI 改为纯 REST 轮询`

## 变更背景
- 线上环境经常已有其它服务占用 `8000`，Monitor/V2 统一改用 `8010`。
- 初期只需要关注 BTC/ETH，后续币种由外部系统通过 API 动态添加。
- 需要本地持久化 watchlist，确保服务重启后能继续关注“仍在 24h TTL 内”的币种。
- Web UI 不再依赖 WebSocket，避免网络环境对 WS 的限制。

## 主要改动
- `core/services/arbitrage_monitor_v2/api/runtime.py`
  - 新增 `SqliteWatchlistStore`，将 watchlist 落盘到 `data/monitor_v2_watchlist.sqlite3`
  - 启动时优先从 SQLite 恢复未过期条目，并自动重新订阅
  - 默认基线仅永久关注 `BTC-USDC-PERP`、`ETH-USDC-PERP`
  - 不再按配置文件 `symbols` 做全量订阅；改为“watchlist 驱动”的动态订阅
- `core/services/arbitrage_monitor_v2/api/web_ui.py`
  - 前端改为只调用 `GET /ui/data` 轮询，不再建立 WebSocket 连接
- `docs/monitor/PLAN.md`
  - 记录 8010 端口约定、默认 BTC/ETH、SQLite 持久化与重启恢复策略

## 接口使用（关键）
- 新增关注（默认 24h）：`POST /watchlist/add`（`ttl_seconds=86400`）
- 续命（单所单币）：`POST /watchlist/touch`（`ttl_seconds=86400`）
- 查看当前存活：`GET /watchlist`

## 验证方式（手工）
- 启动：`./.venv-py312/bin/python run_monitor_service.py --host 0.0.0.0 --port 8010 --config <config>`
- 探活：`curl http://127.0.0.1:8010/health`
- UI：`http://<host>:8010/ui`（纯 REST 轮询）
- 持久化验证：
  1) `POST /watchlist/add` 添加一个新币种（如 SOL）
  2) 重启服务
  3) `GET /watchlist` 确认 SOL 仍存在且 TTL 未过期

