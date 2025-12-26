# 订单簿/价差数据采集（Monitor / V2）Plan

## 1. 目标（面向后续交易系统）
我们的目标是把当前的 Monitor/V2 监控链路，沉淀为一个可复用的“数据层”，为后续交易系统持续提供：
- **低延迟、稳定的订单簿数据**：至少 BBO（best bid/ask），可扩展到多档深度。
- **可用的价差数据**：跨交易所（以及后续的多腿）价差计算结果，可用于机会发现与风险约束。
- **可观测与可度量**：可以量化“延迟/积压/丢弃/断连/数据陈旧”，用于扩容与排障。
- **兼容历史落盘**：价差/关键行情可以异步写入数据库（SQLite/CSV），且不拖慢实时链路。
- **可服务化**：最终能以 Headless（无 UI）方式运行，并向其它仓库/进程提供 snapshot/stream。

## 2. 非目标
- 不讨论实盘执行（下单/风控闭环）的策略细节；本文件聚焦“数据采集与分析链路”。
- 不承诺枚举所有交易所错误码；只记录在规模压测中出现过、并会影响“低延迟稳定性”的问题。

## 3. 数据契约（给交易系统对接用）
### 3.1 订单簿（核心输入）
- 数据模型：`core/adapters/exchanges/models.py` 的 `OrderBookData`
- 最低可用字段（BBO 即可开始价差计算）：
  - `symbol`
  - `bids[0]` / `asks[0]`（`price/size`）
  - `received_timestamp`（本机接收时间，用于延迟/陈旧检测）
- 推荐补齐字段（用于“数据新鲜度”与对齐交易所时间）：
  - `exchange_timestamp`（交易所时间戳，若交易所提供）
  - `processed_timestamp`（入内存缓存时间，衡量本机处理链路）

### 3.2 行情/资金费率（重要但非价差主输入）
- 数据模型：`core/adapters/exchanges/models.py` 的 `TickerData`
- 在 Monitor/V2 中的典型用途：
  - **资金费率展示/记录**（订单簿价差之外的关键风控输入）
  - 其它辅助指标（mark/index/last 等，取决于交易所适配器）

### 3.3 价差（核心输出）
- 数据模型：`core/services/arbitrage_monitor_v2/analysis/spread_calculator.py` 的 `SpreadData`
- 输出语义（对接交易系统时建议保持稳定）：
  - `exchange_buy`/`exchange_sell`：在哪买、在哪卖
  - `price_buy`/`price_sell`：BBO 价格
  - `spread_abs`/`spread_pct`：差价与百分比
- 后续建议补齐的“可交易性字段”（Roadmap）：
  - 价差数据的时间戳链路（对应订单簿的 `received_timestamp`）
  - 深度可成交量（基于多档订单簿估算，而不仅仅是 ask1/bid1）

## 4. 架构与性能策略（现状）
### 4.1 数据流（主路径）
当前监控链路的主路径是：
1) 交易所 Adapter（WS/REST）→ 2) `DataReceiver`（回调入队）→ 3) `asyncio.Queue` → 4) `DataProcessor`（drain 入缓存）→ 5) `SpreadCalculator` → 6) `OpportunityFinder`/UI/历史记录

关键文件：
- 数据接收/队列：`core/services/arbitrage_monitor_v2/data/data_receiver.py`
- 队列消费/缓存：`core/services/arbitrage_monitor_v2/data/data_processor.py`
- 价差计算：`core/services/arbitrage_monitor_v2/analysis/spread_calculator.py`
- 调度器：`core/services/arbitrage_monitor_v2/core/orchestrator.py`

### 4.2 低延迟策略（为什么不会被 UI 拖慢）
- **回调最小化**：WS 回调只做必要验证 + `put_nowait` 入队。
- **队列满“丢旧保新”**：高压时优先保证“最新快照可用”，而不是保证全量消息都处理。
- **时间片 drain**：`DataProcessor` 以时间预算批量消费，避免单轮循环阻塞太久。
- **压测时隔离 UI/分析干扰**：使用独立工具只测“接收→入队→处理”链路的延迟与积压。

### 4.3 关键参数（默认值/常用口径）
- 监控配置模型：`core/services/arbitrage_monitor_v2/config/monitor_config.py` 的 `MonitorConfig`
- 队列容量（默认）：
  - `orderbook_queue_size=1000`
  - `ticker_queue_size=500`
  - `analysis_queue_size=100`
- 调度节奏（默认）：
  - `analysis_interval_ms=10`（目标 100Hz，实际取决于数据量与 CPU）
  - `ui_refresh_interval_ms=1000`（UI 1Hz，仅显示快照，不代表接收频率）
- 订阅规模估算（用于评审“90 币种是否超载”）：
  - 仅 orderbook：`exchanges × symbols`
  - orderbook + ticker：约 `exchanges × symbols × 2`

## 5. 数据落盘（兼容数据库/历史回放）
Monitor/V2 具备异步历史记录能力（避免阻塞主链路）：
- 记录器：`core/services/arbitrage_monitor_v2/history/spread_history_recorder.py`
- 配置入口：`core/services/arbitrage_monitor_v2/config/monitor_config.py`（`spread_history_*` 一组参数）
- 默认落盘目录：`data/spread_history/`（含 `spread_history.db` 与原始 CSV）

工程原则（面向“低延迟 + 可记录”）：
- 主链路只做“轻量 enqueue/采样”，写盘由后台任务批量完成。
- 记录频率（默认 60s）与批量写入参数必须可配置，避免高频写盘导致抖动。

## 6. WS-only 规模压测：口径、结论、异常
### 6.1 测试口径（避免 UI/分析干扰）
- 压测工具：`tools/perf_stream_benchmark.py`
  - 仅测 `DataReceiver -> Queue -> DataProcessor`，输出队列长度/丢弃数/RPS/本地处理延迟（`received_at -> processed_at`）
  - 注意：这是“本机处理延迟”，不等价于“交易所到你这里的网络延迟/撮合延迟”
- WS-only 配置（用于真实评估 WS 健康度，跳过轮询型/易触发 WAF 的交易所）：
  - `config/arbitrage/monitor_v2_ws_only_90.yaml`
  - `config/arbitrage/monitor_v2_ws_only_45.yaml`

### 6.2 90 币种规模（WS 健康度）结论
在 WS-only 场景下，主要结论是：
- **单所/双所的稳态**可以保持 ms 级处理延迟（典型 p95 在几十 ms 量级），未见持续性“秒级积压”。
- **全交易所 90 币种**会暴露“启动阶段/连接策略/回退策略”的边界问题：同一份代码在小规模正常，但在 90 币种时更容易触发握手风暴、REST 限频、心跳缺失、退出资源回收不彻底等问题。

### 6.3 90 币种下出现过的异常（及处理方向）
1) **Variational：REST/WAF（不适合作为 WS 健康度评估对象）**
   - 现象：`403 Forbidden / Just a moment...`（疑似 WAF/Cloudflare）
   - 处理：WS-only 压测配置中先排除（不监听、不轮询），避免“轮询风暴 + 海量日志”干扰结论。

2) **Binance：握手超时（handshake timeout）**
   - 现象：90 币种订阅时出现 `timed out during opening handshake`，随后进入重连/回退路径。
   - 根因（规模触发）：PERP 符号被当成现货流处理，导致“每币一个 WS 连接”的握手风暴；连接数/并发握手上来后更容易超时。
   - 已采用的最小改动修复思路（可回溯）：把 PERP 统一走期货 WS（单连接 + multi-stream SUBSCRIBE），避免 90 个并发握手。

3) **OKX：启动阶段 REST 限频（50011）**
   - 现象：启动/订阅阶段日志出现 `Too Many Requests (50011)`，即便我们“想测 WS”也会被 REST 初始化/兜底影响。
   - 处理策略（用户认可的方案）：
     - **只在启动时“慢慢地”load_markets 一次**（限频、串行、带 backoff）
     - **禁止 WS 抖动时回退到 REST 轮询**（避免按 symbol 拉起轮询风暴）
   - 配置参考：`config/exchanges/okx_config.yaml`（`startup_load_markets: true` + `ccxt_rate_limit_ms: 1000` + `allow_polling_fallback: false`）

4) **GRVT：心跳缺失导致健康检查失败**
   - 现象：报错提示 `_do_heartbeat` 未实现（长跑时更容易暴露）
   - 处理：补齐心跳实现，避免“连接其实可用但被健康检查判死”。

5) **退出/资源回收：disconnect 超时、aiohttp session 未关闭**
   - 现象：部分交易所 `disconnect()` 可能超时，偶见 `Unclosed client session`
   - 说明：这类问题不一定影响“实时稳态延迟”，但会影响长期运行与自动化重启，需要单独跟踪修复。

### 6.4 45 币种复测（降低规模验证 OKX 启动策略）
在 `config/arbitrage/monitor_v2_ws_only_45.yaml` 下复测的主要结论：
- OKX 在“启动期慢速 load_markets + 禁止轮询回退”策略下，未再观察到 `50011 Too Many Requests`。
- 稳态队列长度多数窗口接近 0，订单簿/行情的本机处理延迟仍为 ms 级；偶发短暂堆积会自行恢复。

## 7. 配置与运行建议（面向低延迟/稳定）
### 7.1 WS-only（评估 WS 健康度）
- 首选 WS-only 配置进行规模评估，避免“REST 兜底/轮询”把问题变成 I/O 风暴：
  - `config/arbitrage/monitor_v2_ws_only_90.yaml`
  - `config/arbitrage/monitor_v2_ws_only_45.yaml`

### 7.2 OKX：启动慢速 load_markets + 禁止轮询回退
- 推荐配置：`config/exchanges/okx_config.yaml`
  - `startup_load_markets: true`
  - `ccxt_rate_limit_ms: 1000`
  - `allow_polling_fallback: false`

### 7.3 Binance：PERP 统一走 futures WS（避免握手风暴）
- 原则：永续合约不要按“现货每币直连”的方式订阅，90 币种时会放大并发握手/重连压力。

## 8. Roadmap（从监控脚本到可复用数据服务）
### Phase A：明确“数据服务”输出与健康度指标
- 输出：每个 `exchange+symbol` 的最新 BBO/深度、接收时间、陈旧度（staleness）。
- 指标：队列积压、丢弃数、处理延迟分位数、断连/重连次数、订阅成功率。

### Phase B：Headless 运行 + API（供其它仓库调用）
- 让监控系统可以 `--no-ui` 真正无 UI 运行，并提供：
  - `GET /health`：订阅规模/延迟/断连/陈旧度
  - `GET /snapshot`：当前 BBO/价差/机会快照
  - `WS /ws/stream`：低频推送（价差/机会/延迟快照），基于后台缓存结果，避免额外计算开销
  - `GET /ui`：轻量网页（无依赖前端），用于随时查看实时价差（默认 1Hz 推送）

### Phase C：历史落盘与回放增强（不牺牲实时性）
- 明确“写盘频率/字段/保留策略”，保证写盘压力与实时链路隔离。
- 在不需要资金费率等信息时允许跳过部分元数据加载；需要时在启动阶段慢速补齐一次并缓存。

## 9. 后续实施计划：对外端口 + 动态 watchlist（FR_monitor 联动）
> 目标：把 Monitor/V2 变成“可被其它服务调用的数据源”，同时支持外部动态下发关注币种，并按 24h TTL 自动维护订阅生命周期。

### 9.1 对外数据端口（第一优先级）
- 形式：FastAPI（HTTP + 可选 WebSocket stream），Headless 后台运行 orchestrator
- 最小可用接口（MVP）：
  - `GET /health`：运行状态、各交易所连接状态、队列积压/丢弃、处理延迟分位数
  - `GET /snapshot`：返回 `exchange+symbol` 的最新 BBO、时间戳链路、价差/机会（可按参数过滤）
  - `WS /ws/stream`：网页/其它服务用的低频推送（默认 1Hz，支持 query 参数调整）
  - `GET /ui`：内置 Dashboard（浏览器打开即可），用于实时查看价差/机会（尽量不占用性能）

### 9.2 动态 watchlist 端口（第二优先级）
- 需求：FR_monitor 把“发现的币种/交易所”发给本服务，本服务开始订阅并持续 24h；若同一交易所再次触发则延长存活时间。
- 建议接口（MVP）：
  - `POST /watchlist/add`：按 `symbol`（可选指定 `exchanges`）新增关注，并设置 `ttl_seconds`（默认 86400）
  - `POST /watchlist/touch`：按 `(exchange, symbol)` 延长 TTL（用于“同交易所再次触发则续命”）
  - `POST /watchlist/remove`：手动结束关注（可选指定 exchanges）
  - `GET /watchlist`：查看当前关注列表、到期时间、剩余 TTL、订阅状态

### 9.5 使用方式（服务化 + 可选命令行 UI）
- 仅服务化（推荐用于评估健康度/低延迟）：`./.venv-py312/bin/python run_monitor_service.py --config config/arbitrage/monitor_v2_ws_only_45.yaml`
- 同时启用原先 Rich 命令行界面（会有少量终端渲染开销）：`./.venv-py312/bin/python run_monitor_service.py --enable-cli --config config/arbitrage/monitor_v2_ws_only_45.yaml`

### 9.6 Web Dashboard（低性能开销原则）
> 目标：把“原命令行界面关注的核心信息（价差/机会）”搬到浏览器里，便于随时查看，同时尽量不引入额外 CPU/内存开销。

- 关键原则：**网页只消费后台已有的分析缓存**（`orchestrator.get_latest_analysis()`），不触发额外的价差计算或订单簿处理。
- 推送方式：
  - 首选 `WS /ws/stream`：低频推送（默认 1Hz），每次推送只发送 Top-N（可通过 query 参数调节）。
  - 自动降级 `GET /ui/data`：当浏览器所在网络环境拦截/超时 WebSocket 握手（Upgrade）时，网页会自动切换为 HTTP 轮询（同样默认 1Hz），保证“看得到数据”。
  - 网页标签页隐藏时自动暂停请求（减少无意义开销）。
- 常用入口：
  - `GET /ui`：Dashboard 页面
  - `GET /ui/data`：Dashboard 轮询数据源（与 WS 同结构的 snapshot）
  - `WS /ws/stream`：Dashboard 推送数据源

### 9.7 近期问题与修复（与 90 币种规模化相关）
- **分析循环空跑/页面全空**：出现 `can't subtract offset-naive and offset-aware datetimes`（交易所时间戳的 tzinfo 混用导致时效性检查抛异常），会使分析循环无法产出 `last_analysis_at/symbol_spreads`，UI 自然显示为空。
  - 修复方向：在 `DataProcessor.get_orderbook()` 的“时效性检查”中统一用 timestamp 秒数计算；对“naive 但语义为 UTC”的交易所时间戳按 UTC 解释，避免误判过期。
- **OKX WS 重连异常**：在 websockets v15 中连接对象不再有 `.closed` 属性，旧代码会在重连/状态检查时异常。
  - 修复方向：增加 `_ws_is_open()` 做版本兼容判断（同 Binance 的处理方式）。
- **服务启动卡住（starting=true 很久）**：个别交易所订阅调用可能因网络/交易所问题长时间不返回，导致启动阶段阻塞。
  - 修复方向：订阅阶段增加超时保护（不改变“优先 WS、不回退轮询”的原则，只防止系统被单点阻塞拖死）。

### 9.3 生命周期与订阅策略（避免 90 币种下的订阅/回退风暴）
- 关注粒度：以 `(exchange, symbol)` 为单位维护 `expire_at`（满足“同交易所触发才延寿”）
- 自动回收：后台任务周期性清理过期项，并尝试 `unsubscribe`（若交易所不支持 unsubscribe，则至少“停止入队/停止参与分析”）
- WS-only 原则：默认不允许“WS 短暂断开就回退 REST 轮询”，避免规模场景下 I/O 风暴（OKX 已验证）
- 订阅并发控制：动态新增时按交易所分批/限速订阅，避免握手风暴（Binance 已验证）

### 9.4 状态持久化（建议尽早加，避免重启丢关注）
- watchlist 持久化：SQLite（同库或单独表），重启时恢复未过期条目并自动重新订阅
- 价差/资金费率历史：沿用现有 `SpreadHistoryRecorder` 的异步写盘策略

## 附录 A：安全审计结论（仓库后门/密钥外传视角）
> 目的：回答“这个仓库是否存在把 API Key/私钥联网发给黑客”的后门风险。

- **审计范围**：本仓库源码 + 本仓库虚拟环境已安装依赖的“来源核对与离线扫描”；不包含操作系统层面、浏览器插件、终端录屏、剪贴板等外部风险。
- **结论（当前版本）**：未发现“读取环境变量/`.env`/配置中的密钥 → 发往第三方（webhook/pastebin/陌生域名等）”的明确后门逻辑；仓库内网络访问主要面向交易所官方 API 域名与本地服务端口。
- **发现的主要风险点（非后门）**：
  - **供应链风险**：依赖里存在从 GitHub 直装的 SDK（见 `requirements.txt`），即使已固定 commit，仍属于“第三方代码可联网执行”的风险来源；若你不更新并且已固定安装版本，风险面主要集中在“当前安装的这次”。
  - **密钥输出风险**：`tools/query_account_with_apikey.py` 会把用户输入的 `api_key_private_key` 打印到终端用于配置（不是外传，但会扩大泄露面）。

## 附录 B：代码地图（快速定位）
- 入口脚本：`run_arbitrage_monitor_v2.py`
- V2 调度器：`core/services/arbitrage_monitor_v2/core/orchestrator.py`
- 数据接收/入队：`core/services/arbitrage_monitor_v2/data/data_receiver.py`
- 数据处理/缓存：`core/services/arbitrage_monitor_v2/data/data_processor.py`
- 价差计算：`core/services/arbitrage_monitor_v2/analysis/spread_calculator.py`
- 机会识别：`core/services/arbitrage_monitor_v2/analysis/opportunity_finder.py`
- 历史记录：`core/services/arbitrage_monitor_v2/history/spread_history_recorder.py`
