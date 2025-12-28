# Monitor/V2（8010）数据服务说明（精简版）

本文件描述：服务运行约定、对外 REST 用法、watchlist 持久化规则，以及已做过的规模/深度压测结论。

## 1. 固定约定
- 服务端口：**8010**
- UI：纯 REST 轮询（不依赖 WebSocket）
- 标准 symbol：`BASE-QUOTE-PERP`（当前主要使用 `*-USDC-PERP`；部分交易所内部会映射到 `USDT`）

## 2. 外部系统如何使用（REST）
### 2.1 运行状态与延迟
- `GET /health`
  - 关注字段：
    - `watchlist_pairs`
    - `stats.data_processor.orderbook_delay_p95_ms` / `ticker_delay_p95_ms`
    - `stats.data_processor.orderbook_queue_size` / `ticker_queue_size`
    - `stats.data_receiver.orderbook_dropped` / `ticker_dropped`

### 2.2 订单簿快照（交易端读取入口）
- `GET /snapshot`
  - 返回：`orderbooks`（按 `exchange -> symbol` 聚合）
  - 当前只提供 BBO：`bid_price/bid_size/ask_price/ask_size`（暂不输出多档）
  - 过滤参数（可重复）：
    - `symbols=BTC-USDC-PERP`
    - `exchanges=okx`
  - 规则：**当不传 `exchanges`（或传空）时，不做交易所过滤，返回所有已关注且有数据的交易所**。

### 2.3 watchlist（关注列表）
- `GET /watchlist`：查看当前关注项（`exchange+symbol` 粒度，含 `expire_at/ttl_seconds`）
- `POST /watchlist/add`
  - 请求：`{"symbol":"SOL-USDC-PERP","exchanges":null,"ttl_seconds":3600}`
  - 规则：`exchanges` 为空表示“所有已配置交易所”
- `POST /watchlist/touch`：延长单个 `(exchange, symbol)` 的 TTL
- `POST /watchlist/remove`：移除关注

## 3. 持久化与重启恢复
### 3.1 持久化位置
- SQLite：`data/monitor_v2_watchlist.sqlite3`

### 3.2 默认 baseline
- 启动时默认永久关注（`expire_at=NULL`）：
  - `BTC-USDC-PERP`
  - `ETH-USDC-PERP`

### 3.3 “全交易所覆盖”建议（保证 snapshot 可返回全量）
为便于后台/交易端查询“同一个 symbol 在所有交易所的订单簿”，建议保持：
- 每个存活 `symbol` 都在全部交易所有对应 watchlist 项（即每个 symbol 覆盖全部 `exchanges`）
- 外部系统新增关注时：调用 `POST /watchlist/add` 且 `exchanges=null`（由服务扩展为全交易所关注）

## 4. 性能口径（如何判断拥堵）
使用 `GET /health`：
- 延迟：`orderbook_delay_p95_ms`、`ticker_delay_p95_ms`
- 积压：`orderbook_queue_size`、`ticker_queue_size`
- 丢弃：`orderbook_dropped`、`ticker_dropped`

经验判断：
- `queue_size` 持续上升 + `p95` 显著抬升（例如 >100ms）通常代表系统内排队，属于拥堵信号。

## 5. 压测结论：45 币种 × 7 交易所，深度 5/10 档是否会拥堵
> 目标：评估订单簿从 BBO 扩展到 5/10 档后，对“接收→入队→处理”的影响。  
> 约束：**不改变对外 8010**（存在外部消费者），压测用独立端口另起实例，仅供本机测量。

### 5.1 方法（概要）
- 使用 `config/arbitrage/monitor_v2_ws_only_45_no_backpack.yaml`（7 交易所、45 symbol）
- 通过 `/watchlist/add` 将 45 个 symbol 全部加入，达到 `45 × 7 = 315 pairs`
- 通过压测实例 `/health` 读取 `p95/queue/dropped` 作为是否拥堵的判据

### 5.2 结论（样本观察）
- depth=5：整体更稳，`orderbook_delay_p95_ms` 多数时间在 **15–40ms**；仍可能出现偶发尖峰（可到 200ms+，并出现短时队列积压）。
- depth=10：更容易出现持续性拥堵/丢弃（队列上千、p95 更高，且出现 orderbook/ticker 的丢弃累计）。

### 5.3 深度能力差异（重要限制）
- OKX：明确支持 `books` / `books5`，不保证 `books10`。
- GRVT：支持档位为 `10/50/100/500`；请求不在集合内会回退（可能反而更重）。
- 不建议用“一个全局深度数字”套全部交易所；更建议“按交易所做档位策略”。

### 5.4 推荐策略（面向 45×7 规模）
- 默认建议：BBO/5 档优先，保证稳定与低延迟。
- 如需 10 档：只对少数关键交易所/关键币种开启，并持续监控 `p95/queue/dropped`。

## 6. 已知限制（当前版本）
- `/snapshot` 暂只返回 BBO（多档订单簿暂未通过 REST 输出）。
- 不同交易所对深度档位/推送频率/增量机制差异很大，容量规划必须以压测数据为准。

## 7. 交易所上架核验（TRU/GAS）
- TRU：仅 Binance 永续有合约；OKX/Hyperliquid/Paradex/GRVT/EdgeX/Lighter 均未发现该合约。
- GAS：Binance/OKX/Hyperliquid 有合约；EdgeX/Lighter/Paradex/GRVT 未发现。
- 结论：不是 USDT/USDC 或斜杠/冒号解析问题，属于交易所未上架。

## 8. Binance SOCKS5 代理兼容（REST + WS）
### 8.1 背景与结论
- 直连 `api.binance.com` / `fapi.binance.com` 返回 451（public + private 均被拦截）。
- 通过 SOCKS5 代理后可恢复访问（REST 与 WS 均可用）。

### 8.2 代理配置与依赖
- 配置项（`config.py`）：
  - `BINANCE_PROXY_URL`：REST 代理（例如 `socks5h://47.79.224.99:1080`）。
  - `BINANCE_WS_PROXY_URL`：WS 代理（为空则回退 `BINANCE_PROXY_URL`）。
- 注意：`config_private.py` 优先级高于环境变量；如果在 `config_private.py` 写了同名字段，会覆盖环境变量。
- 依赖：
  - REST（requests）：`pysocks`。
  - WS（websocket-client）：`python-socks`。

### 8.3 代码改动要点
- `trading/trade_executor.py`：`_send_request()` 对 Binance URL 自动注入代理（其他交易所直连）。
- `exchange_connectors.py`：Binance spot/futures WS 通过 SOCKS5 代理连接。

### 8.4 验证方式（实测）
- 网络验证：
  - `curl --socks5-hostname 47.79.224.99:1080 https://api.binance.com/api/v3/time` 返回 200。
- REST 私有验证：
  - `http://127.0.0.1:4002/api/live_trading/positions?all=1&nonzero=0` 中 `binance` 项无错误。
  - 或 `get_binance_perp_account()` / `get_binance_spot_account()` 直接返回数据。
- WS 验证：
- 通过 SOCKS5 连接 `wss://stream.binance.com:9443/ws/btcusdt@ticker` 可收到消息。
  - 运行中可观察 `ss -tnp | rg '47.79.224.99:1080'` 有长连接，且不再出现 Binance 451 日志。

## 9. watchlist TTL 与订阅清理（近期变更记录）
- 新增全量重置 TTL 接口：`POST /watchlist/reset_ttl`（可统一设置/清空过期时间）。
- 订阅清理补齐：Binance / OKX / EdgeX / GRVT / Hyperliquid（ccxt+native）/ Backpack / Variational 等实现 `unsubscribe_*`，并在无订阅时尝试关闭 WS 连接。
- 轮询降级停止：Hyperliquid 轮询模式按数据类型可停止（避免取消某类订阅导致其它轮询被误停）。
- UI 展示补充：页面展示“当前关注的交易所 / 当前关注的币种清单”，便于确认 watchlist 覆盖范围。
- 健康状态清理：watchlist 变更时裁剪 health 监控的历史 symbol，避免 TTL 清理后仍被判定为 unhealthy。
