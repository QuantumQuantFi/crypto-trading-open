# 套利监控系统（Monitor / V2）Plan 文档

## 目标
- 将“纯监控（仅数据展示）”模式的实现逻辑沉淀为可维护的文档与代码地图。
- 明确数据流、关键模块边界、性能策略（队列、节流、采样）、以及与交易所 REST/WS 的交互面。
- 为后续“服务化（供其它仓库实盘系统调用）”提供可执行的改造路线。

## 非目标
- 不讨论实盘执行（下单/风控闭环）的策略细节；本文件聚焦 Monitor/V2 的监控链路。
- 不承诺对所有交易所的错误码行为做完整枚举（如 418 等），只描述当前代码的处理现状与风险。

## 安全审计结论（仓库后门/密钥外传视角）
> 目的：回答“这个仓库是否存在把 API Key/私钥联网发给黑客”的后门风险。

- **审计范围**：本仓库源码 + 本仓库虚拟环境已安装依赖的“来源核对与离线扫描”；不包含操作系统层面、浏览器插件、终端录屏、剪贴板等外部风险。
- **结论（当前版本）**：未发现“读取环境变量/`.env`/配置中的密钥 → 发往第三方（webhook/pastebin/陌生域名等）”的明确后门逻辑；仓库内网络访问主要面向交易所官方 API 域名与本地服务端口。
- **发现的主要风险点（非后门）**：
  - **供应链风险**：依赖里存在从 GitHub 直装的 SDK（见 `requirements.txt`），即使已固定 commit，仍属于“第三方代码可联网执行”的风险来源；若你不更新并且已固定安装版本，风险面主要集中在“当前安装的这次”。
  - **密钥输出风险**：`tools/query_account_with_apikey.py` 会把用户输入的 `api_key_private_key` 打印到终端用于配置（不是外传，但会扩大泄露面）。
- **已安装的交易所相关 SDK（本机 .venv 观测）**：
  - **Hyperliquid**：`hyperliquid-python-sdk==0.21.0`（PyPI 元数据与 GitHub 仓库 `hyperliquid-dex/hyperliquid-python-sdk` 对应）。
  - **Paradex**：`paradex_py==0.5.2`（PyPI 元数据与 GitHub 仓库 `tradeparadex/paradex-py` 对应；`paradex.trade` 页面包含指向 `github.com/tradeparadex` 的链接）。
  - **EdgeX**：`edgex-python-sdk==0.3.0`（从 `edgex-Tech/edgex-python-sdk` 固定到 commit 安装）。
  - **Lighter**：`lighter-sdk==1.0.2`（从 `elliottech/lighter-python` 固定到 commit 安装，默认 API 域名为 `mainnet.zklighter.elliot.ai`）。

## 结论速览（本次梳理产出）
> 这一节把我们在沟通里确认过的“结论”先落在纸面上，方便后续开发/迁移组件时快速对齐口径。

- **监控范围（当前配置）**：`config/arbitrage/monitor_v2.yaml` 启用 3 个交易所（`edgex/lighter/paradex`）与 4 个 symbols（`BTC/ETH/HYPE/SOL` 的 `*-USDC-PERP`），因此覆盖 `3×4=12` 个“交易所-合约”组合。
- **币种怎么选**：只看 `monitor_v2.yaml` 里 `symbols:` 下“未被 `#` 注释”的条目；注释掉的候选行不会被 YAML 解析进列表，所以运行时是 **完全不订阅**（不是降频）。
- **数据来源（WS vs REST）**：实时行情（orderbook/ticker）链路 **以 WebSocket 为主**；但启动阶段的 markets/exchange_info、以及余额/账户类能力 **可能走 REST**（因交易所适配器实现而异）。
- **推送频率（不是固定 1Hz）**：orderbook/ticker 是 WS 推送，频率由交易所与市场活跃度决定；UI 刷新固定 1Hz 只是“显示快照”频率，不代表后台只接收 1Hz。
- **是否手动指定 WS 刷新频率**：当前三个交易所的订阅消息都 **不带“请求服务器按 XHz 推送”的参数**；能控制的主要是频道/深度（例如 EdgeX 的 `depth.<contract_id>.<depth>`）。
- **高压策略**：队列满/接近满时采用“丢旧保新”，优先保证“看起来接近实时”，而不是保证每一条推送都被处理与展示。
- **ETH 订单簿推送量级（实测样本）**：在 12 秒窗口内观测到 EdgeX ~1.42/s、Lighter ~13.58/s、Paradex ~7.25/s（仅为当时网络与市场活跃度下的观测值，不保证恒定）。

## 性能压测记录（90 币种：WS 健康度与异常）
> 目标：回答“关注 90 个币种时，WS 是否仍能健康快速监听订单簿？会不会明显积压/延迟？”

### 测试口径（避免 UI/分析干扰）
- 使用 `tools/perf_stream_benchmark.py`：仅跑 `DataReceiver -> asyncio.Queue -> DataProcessor`，输出队列长度/丢弃数/RPS/本地处理延迟（`received_at -> processed_at`）。
- 配置文件：
  - `config/arbitrage/monitor_v2_lighter_90.yaml`（单所 90 币种）
  - `config/arbitrage/monitor_v2_edgex_lighter_90.yaml`（双所 90 币种）
  - `config/arbitrage/monitor_v2_all_90.yaml`（全交易所 90 币种，见“异常”）

### 稳态结论（能否“健康快速”）
- **Lighter / 90 币种**：预热后队列基本为 0，未见持续积压；延迟统计为 **ms 级**（p95 约十几 ms，max 约数百 ms 的长尾）。
- **EdgeX+Lighter / 90 币种**：预热后队列回落为 0；延迟统计仍为 **ms 级**，说明消费能力跟得上推送（长尾同样存在但不形成持续堆积）。

### 90 币种下发现的异常（重点）
1. **启动阶段 ticker burst 导致队列满与丢弃（EdgeX+Lighter 组合更明显）**
   - 现象：启动瞬间 `ticker_queue` 达到上限并产生 `ticker_dropped`，之后进入稳态队列回落为 0。
   - 影响：只影响“启动的短时间窗口”的 ticker 完整性；订单簿链路未见持续积压。
   - 处理现状：该链路的设计是“丢旧保新”；为避免异常刷屏已修正 Lighter ticker 满队列时的处理为“丢旧保新 + 统计 dropped”：
     - 代码：`core/services/arbitrage_monitor_v2/data/data_receiver.py`（Lighter ticker 回调的 `QueueFull` 处理）

2. **Lighter ticker 回传符号与配置不一致的告警（单次打印）**
   - 现象：看到 `PAXG-USD-PERP` 等符号不在监控列表的 warning（只打印一次）。
   - 原因：符号标准化/映射导致的“USD/USDC 语义差异”叠加 Lighter 回传的市场集合与本次选择的 90 币种列表不完全重合。
   - 影响：主要是日志噪声，不影响订单簿消费能力；如要消除，可补齐映射或从订阅列表中排除对应市场。

3. **“全交易所 + 90 币种”无法作为有效 WS 压测样本（被非 WS 因素拖垮）**
   - 现象：运行 `config/arbitrage/monitor_v2_all_90.yaml` 时出现大量错误日志并在长时间内无法完成稳定统计。
   - 主要原因（与 WS 性能无关，但会影响压测结果）：
     - **Variational**：出现 `403 Forbidden / Just a moment...`（疑似 WAF/Cloudflare），导致轮询/请求反复失败并产生海量日志，压测输出被淹没。
     - **Binance**：多符号订阅触发 `timed out during opening handshake`，随后回退到轮询模式（额外 I/O 与任务数增加）。
     - **GRVT**：出现心跳实现缺失类错误（`子类必须实现 _do_heartbeat`），提示适配器健康检查路径未完整实现。
   - 建议：若目标是验证“WS 监听订单簿的健康度”，应先排除/隔离上述非 WS 的失败源（例如临时从压测配置移除 variational/binance/grvt，或为 variational 提供可用鉴权/headers），再做跨多所的 90 币种压测。

### WS-only（排除轮询型）90 币种多所压测：新增结论与异常
> 新增配置：`config/arbitrage/monitor_v2_ws_only_90.yaml`（排除 variational，仅保留具备 WS 行情能力的交易所）

#### 结果概览（关键观察）
- **订阅规模**：8 个交易所 × 90 个币种（实际各所支持度不同，订阅失败会被静默跳过）
- **现象**：在启动阶段与进入统计窗口后，`orderbook_queue/ticker_queue` 均出现明显堆积，延迟统计进入 **秒级**（说明“单机单进程 drain 策略”在该规模下开始吃紧）。
  - 示例输出（一次运行样本）：`Q(ob/tk)` 峰值接近 `38478/20000`，统计窗口内仍可见 `Q(ob/tk)` 数千级，`delay_ms(p95)` 达到 2s+。

#### 90 币种多所下暴露的新问题（与 10 币种差异最大的点）
1) **Binance：原先因为“每币一个现货 WS 直连”导致握手超时**
   - 现象：旧行为会出现 `timed out during opening handshake`，并触发“降级到轮询”，导致日志与网络压力激增。
   - 根因：标准 PERP 符号在 Binance 被转换成 `BTCUSDT` 形态，WS 层按“现货直连”处理（每币一个 WebSocket），90 币种就变成 90 次握手风暴。
   - 处理：将 Binance 的 PERP 符号输出改为 `BTC/USDT:USDT`，强制走期货 WS（单连接 + SUBSCRIBE 多流），并补齐期货 orderbook 的订阅分支。

2) **OKX：启动/订阅阶段出现 REST 限频（即使我们“想测 WS”）**
   - 现象：日志出现 `Rate limit reached / Too Many Requests (50011)`。
   - 含义：OKX 适配器在连接/订阅过程中仍会调用部分 REST（例如市场信息、辅助接口或失败 fallback），在 90 币种规模下更容易触发限频，从而影响整体压测“纯 WS”结论。

3) **资源回收与退出稳定性**
   - 现象：某些交易所 `disconnect()` 会超时（例如 grvt/paradex），且仍可能出现 `Unclosed client session`（aiohttp session 未完全释放）。
   - 含义：在“大规模订阅 + 多适配器”场景下，退出路径更容易暴露“任务取消/连接释放不彻底”的边界问题，需作为后续稳定性修复项单独跟踪。

### WS-only 45（降低规模）复测：OKX 启动限频 + 多所稳态延迟
> 新增配置：`config/arbitrage/monitor_v2_ws_only_45.yaml`（从 90 币种裁剪到 45 币种，用于观察规模下降后的吞吐/延迟）

- **目标**：验证“OKX 启动阶段的 REST 限频（50011）”是否可规避，并观察 8 所×45 币种是否仍出现秒级积压。
- **OKX 修复点（简单可回溯）**：
  - `OKXRest.initialize()` 在“无鉴权（仅公共行情监控）”场景默认不强制 `load_markets`，并设置更保守的 `ccxt.rateLimit` 与 50011 的 backoff（`core/adapters/exchanges/adapters/okx_rest.py`）。
  - `OKXAdapter.subscribe_*()` 逻辑调整为“直接尝试 WS 订阅；失败时默认跳过（除非显式开启轮询 fallback）”，避免启动时因 `is_connected==False` 误判而为每个 symbol 启动 REST 轮询（`core/adapters/exchanges/adapters/okx.py`）。
- **OKX 配置建议（两者兼得：只在启动时慢速 load_markets + 禁用轮询回退）**：
  - `config/exchanges/okx_config.yaml`：
    - `startup_load_markets: true`
    - `ccxt_rate_limit_ms: 1000`（更保守节流）
    - `startup_fetch_time: false`（可选）
    - `allow_polling_fallback: false`（避免 WS 抖动时触发轮询风暴）
- **结果（一次运行样本，warmup=8s 后统计 35s）**：
  - **OKX**：未再出现 `50011 Too Many Requests`；也未再出现“因轮询触发的 does not have market symbol”类 REST 错误刷屏。
  - **订单簿**：稳态 `Q(ob)` 基本为 0；`delay_ms(ob p95)` 多数窗口在 **~25–55ms**，偶发抖动到 **~135ms**，`max` 约 **< 1s**（单次尖峰）。
  - **Ticker**：预热阶段有明显长尾（启动 burst）；预热后 `delay_ms(tk p95)` 多数窗口在 **~30–72ms**，队列基本为 0。
  - **丢弃**：统计窗口内 `drop(ob/tk)=0/0`（本次样本未触发队列丢弃策略）。
- **结果（启用“启动期慢速 load_markets”后再次复测：warmup=10s 后统计 40s）**：
  - **OKX**：在开启 `startup_load_markets: true` + `ccxt_rate_limit_ms: 1000` 的情况下，仍未观察到 `50011 Too Many Requests`。
  - **整体稳态**：绝大多数统计窗口队列接近 0，未触发丢弃（`drop(ob/tk)=0/0`）；订单簿延迟仍为 **ms 级**（p95 多在 **~30–96ms**）。
  - **偶发抖动**：某窗口出现 `Q(ob/tk)~993/813` 的短暂堆积，但随后恢复到 0（未形成持续秒级积压）。
  - **退出稳定性（仍需跟踪）**：运行尾部仍可能出现 `disconnect()` 超时（如 paradex/binance）与 `Unclosed client session`（aiohttp session 未完全释放）。

## 当前的说明（现状梳理）
> 这一章是对“它用哪些代码如何实现、是否会触发 418、能否服务化”的当前实现说明。

### 入口与配置
- 入口脚本：`run_arbitrage_monitor_v2.py`
  - 读取配置：默认 `config/arbitrage/monitor_v2.yaml`
  - 启动：创建 `ArbitrageOrchestrator` 并 `await orchestrator.start()`
  - 参数：支持 `--config/--debug/--debug-detail/--symbols/--no-ui`
    - 注意：`--no-ui` 参数目前在入口层定义，但未完整接入为“真·无UI模式”（属于可改造点）。

### 总体架构（按数据流）
监控链路核心是“**WS 采集 → 内存缓存 → 价差计算 → 机会过滤 → Rich UI 展示**”，并通过队列与节流避免阻塞。

1. **交易所适配器创建与连接**
   - 工厂：`core/adapters/exchanges/factory.py`（按 `exchange_id` 创建 `edgex/lighter/paradex/...`）
   - 调度器在 `start()` 中完成：
     - 初始化适配器、并行连接
     - 注册到数据接收层
     - 订阅市场数据（symbols）

2. **数据接收层（零延迟入队）**
   - `core/services/arbitrage_monitor_v2/data/data_receiver.py`
   - 原则：回调只做最小验证 + `put_nowait` 入队，队列满时丢旧保新（保证实时性，不追求全量）。
   - 符号统一：使用 `SimpleSymbolConverter` 将各交易所 symbol 转为标准格式。

3. **数据处理层（维护最新缓存）**
   - `core/services/arbitrage_monitor_v2/data/data_processor.py`
   - 原则：独立协程从队列批量 drain，在时间片内尽可能清空，维护：
     - `orderbooks[exchange][symbol]`
     - `tickers[exchange][symbol]`
     - 对应时间戳（用于过期检测与统计）

4. **价差计算**
   - `core/services/arbitrage_monitor_v2/analysis/spread_calculator.py`
   - 做法：按交易所两两组合，对每一对计算双方向价差（A 买 B 卖；B 买 A 卖），得到 `SpreadData` 列表。

5. **机会识别与持续时间跟踪**
   - `core/services/arbitrage_monitor_v2/analysis/opportunity_finder.py`
   - 做法：
     - 用阈值（如 `min_spread_pct`）过滤
     - 将满足条件的 spread 维护为“机会”，跟踪 `first_seen/last_seen/duration`
     - 并按 spread 排序供 UI 展示

6. **UI 展示（Rich）**
   - UI 管理：`core/services/arbitrage_monitor_v2/display/ui_manager.py`
   - UI 组件：`core/services/arbitrage_monitor_v2/display/ui_components.py`
   - 布局：Header（系统状态/性能/风险）+ Body（价格表/机会表等）+ Scroller（滚动区域）
   - UI 更新策略：调度器侧对“重数据（全量订单簿/全量ticker采样）”做节流/抽样，降低卡顿风险。

### REST vs WebSocket：何时用谁（当前实现）
很多人会把“监控系统”理解成“全程只用 WS”，但本仓库的 Monitor/V2 更准确的描述是：**实时行情链路 WS 为主，REST 为辅（用于启动与账户类能力）**。

#### 1) 实时行情（核心监控数据）：主要来自 WebSocket
- 监控模式的核心数据是 **OrderBook / Ticker**，由调度器调用 `DataReceiver.subscribe_all(self.config.symbols)` 发起订阅（`core/services/arbitrage_monitor_v2/core/orchestrator.py` → `core/services/arbitrage_monitor_v2/data/data_receiver.py`）。
- `DataReceiver` 通过各交易所适配器的 `subscribe_orderbook/subscribe_ticker` 注册回调；具体实现走各交易所的 websocket 模块（例如 `core/adapters/exchanges/adapters/edgex_websocket.py` / `lighter_websocket.py` / `paradex_websocket.py`）。
- 因此：**UI 表格里的买1/卖1（BBO）、价差计算用到的数据，来自 WS 推送并进入队列/缓存。**

#### 2) 启动阶段/元数据：可能会用 REST（即便监控本身主要靠 WS）
不同交易所适配器在 `connect()` 阶段会做一些“初始化工作”，其中常见的是：
- 建立 REST session（为后续可能的查询做准备）。
- 获取 markets / exchange_info（用于符号/市场映射、精度、能力探测等）。

在当前实现里可看到三个交易所的典型行为差异：
- **Paradex**：连接时先 REST `connect()`，尝试 `get_exchange_info()`，再连接 WebSocket（`core/adapters/exchanges/adapters/paradex.py`）。
- **Lighter**：连接时会“尝试初始化 REST + 加载 market_info”，但即使 REST 初始化失败也会继续建立 WS（公共数据订阅只依赖 WS，`core/adapters/exchanges/adapters/lighter.py`）。
- **EdgeX**：连接时会先 `rest.setup_session()` 再连 WS，并通过 WS 获取支持的交易对后同步给 REST 模块（`core/adapters/exchanges/adapters/edgex.py`）。

结论：**行情订阅本身主要靠 WS，但启动时是否/如何触发 REST，取决于交易所适配器的连接实现。**

#### 3) 账户/资金类数据：通常来自 REST（纯监控 UI 未必会用到）
仓库里与“余额/持仓/订单”等相关的接口通常走 REST（或需要签名/JWT），例如：
- 风险控制/余额检查会调用 `adapter.get_balances()`（多为 REST 查询，见 `core/services/arbitrage_monitor_v2/risk_control/global_risk_controller.py`）。

这类调用是否发生，取决于你运行的是“纯监控”还是带账户/风控/执行的编排模式；但从代码能力上看，**系统确实存在 REST 访问面**。

### 定量指标（当前配置 + 机制上限）
本小节尽量给出“可量化”的口径；其中有两类数字：
- **配置驱动的确定值**：由 `config/arbitrage/monitor_v2.yaml` 与 `MonitorConfig` 决定。
- **运行时变量**：取决于交易所推送频率、网络、机器性能；文档给出“代码上限/目标节奏”和可观测点。

#### 订阅覆盖规模（当前 `monitor_v2.yaml`）
- 一句话结论：**当前运行时会关注 3 个交易所 × 4 个合约（币种）= 12 组“交易所-合约”组合**。
- 交易所数量：3（`edgex`, `lighter`, `paradex`，见 `config/arbitrage/monitor_v2.yaml`）
- 启用的监控交易对：4（`BTC/ETH/HYPE/SOL` 对应的 `*-USDC-PERP`）
- 在文件中列出的候选交易对：47，其中 43 行处于注释状态（不会订阅，不会进入 WS 回调链路）
- 这些“4 个币种/合约”**怎么选出来的**（决定运行时到底关注谁）：
  - **唯一来源（当前实际生效）**：`config/arbitrage/monitor_v2.yaml` 的 `symbols:` 列表里「没有被 `#` 注释」的条目。
  - YAML 的注释行（以 `#` 开头）不会被 `yaml.safe_load()` 解析进 `symbols` 数组，所以**注释掉 = 程序完全看不见 = 也就完全不会订阅**。
  - 入口脚本 `run_arbitrage_monitor_v2.py` 会把 `symbols` 原样交给调度器，调度器在 `core/services/arbitrage_monitor_v2/core/orchestrator.py` 的 `_subscribe_data()` 里执行 `subscribe_all(self.config.symbols)`。
  - 注意：`run_arbitrage_monitor_v2.py` 的 `--symbols` 参数目前**只用于 DebugConfig 的调试过滤**，并不会覆盖 `monitor_v2.yaml` 的订阅列表（容易误解，属于可改造点）。
  - 另外：`ConfigManager` 内部有 `extra_symbols` / `multi_leg_pairs` 的“附加订阅 symbol”能力（见 `core/services/arbitrage_monitor_v2/config/monitor_config.py`），但当前 `ArbitrageOrchestrator` 仍然只订阅 `self.config.symbols`，未使用 `get_subscription_symbols()`（同样属于可改造点）。
- 订阅流数量（近似口径）：
  - OrderBook：`exchanges × symbols = 3 × 4 = 12` 条
  - Ticker：`exchanges × symbols = 3 × 4 = 12` 条
  - 合计约 `24` 条实时流（不含私有频道/用户数据）
  - 统计观测（如何“运行起来验证不是拍脑袋”）：
    - 启动时控制台会打印：`📊 监控交易所: ...` 与 `💰 监控代币: ...`（见 `core/services/arbitrage_monitor_v2/core/orchestrator.py:start()`）。
    - UI 顶部状态栏也会显示“监控代币: N 个”；N 就是 `monitor_v2.yaml` 里启用的 `symbols` 数量。
    - 若要更细的量：`DataReceiver.stats` 记录了 `orderbook_received/ticker_received` 与 `*_dropped`，可用于计算“每秒接收/丢弃多少”（见 `core/services/arbitrage_monitor_v2/data/data_receiver.py`）。

#### 队列与节流（确定值）
来自 `core/services/arbitrage_monitor_v2/config/monitor_config.py` 与 `config/arbitrage/monitor_v2.yaml`：
- 队列容量：
  - OrderBook 队列 `1000`
  - Ticker 队列 `500`
  - Analysis 队列 `100`（监控模式主要用于内部结构，实际热点在前两者）
- 分析循环目标频率：`analysis_interval_ms=10` → **目标 100Hz**（实际取决于 CPU 与数据量）
- UI 刷新频率：`ui_refresh_interval_ms=1000` → **1Hz**（UI 只显示抽样后的快照，不等于接收频率）
- DataProcessor drain 时间片：每个循环对每个队列 **最多消费 5ms**（`time_budget=0.005`），空转时 `sleep(1ms)`

#### 时间戳精度（确定值 + 运行时来源差异）
- 本地时间戳精度：Python `datetime.now()`（微秒级分辨率）
- OrderBook 时间戳链路：
  - `exchange_timestamp`：若适配器在 `OrderBookData` 内提供，则在接收时被透传
  - `received_at/received_timestamp`：接收回调入队时打点
  - `processed_timestamp`：`DataProcessor` 处理入缓存时打点
- 重要区别：
  - **套利价差计算/表格展示**以 OrderBook 的 **BBO（best_bid/best_ask）** 为准
  - **Ticker（含 last/mark/index 等）在监控 V2 中主要用于资金费率展示**，不是价差计算的主数据源

#### “订单簿多久更新一次？”（精度/频率的正确口径）
这套系统**不会**像 REST 轮询那样“每 1 秒请求一次”。核心原因是：监控数据是 **WebSocket 推送驱动**，所以频率由“交易所推送速度 + 你订阅的频道类型 + 市场活跃度”共同决定。

把“更新频率”拆成 3 个层次理解会更清晰：
1) **交易所→本机（真实接收频率）**  
   - OrderBook/Ticker 消息是“来一条处理一条”，可能是 **每秒多次**、也可能在冷门市场时 **几秒才来一条**。  
   - 本仓库没有把 orderbook/ticker 的频率写死成 1Hz/10Hz；因此“每秒更新一次”不是准确描述。

2) **本机内部处理（缓存更新频率）**  
   - `DataReceiver` 收到 WS 回调就入队（队列满则丢旧保新，`core/services/arbitrage_monitor_v2/data/data_receiver.py`）。  
   - `DataProcessor` 持续 drain 队列，把最新订单簿/行情写入内存缓存；每次 drain 对每个队列给 5ms 时间片，空转 `sleep(1ms)`（`core/services/arbitrage_monitor_v2/data/data_processor.py`）。  
   - 因此：在不积压时，缓存更新频率通常接近“真实接收频率”；在高压时会“丢旧保新”，表现为**隐式降采样（保留最新快照）**。

3) **终端 UI 刷新（你“看到”的频率）**  
   - UI 刷新是固定的 `ui_refresh_interval_ms=1000` → **1Hz**（`config/arbitrage/monitor_v2.yaml` / `MonitorConfig`）。  
   - 这意味着：即便后台每秒收了 50 次 orderbook，你在 UI 上也可能只看到“每秒跳一次”的快照更新；**UI 的 1Hz 不等于数据只 1Hz**。

补充：**当前适配器不会在订阅时手动指定“推送频率”**（如果交易所支持按固定频率推送，那也是交易所协议层能力；本仓库未使用该类参数）：
- EdgeX：订阅消息是 `depth.<contract_id>.<depth>`，这里的 `depth` 是深度，不是推送频率（`core/adapters/exchanges/adapters/edgex_websocket.py`）。
- Paradex：订阅 `bbo.<symbol>`，订阅参数仅包含 `channel`（`core/adapters/exchanges/adapters/paradex_websocket.py`）。
- Lighter：订阅 `market_stats/<id>` 与 `order_book/<id>`，订阅参数只有 `channel`；代码里的 `sleep(0.1)` 仅用于“发送订阅别太快”，不是控制服务器推送频率（`core/adapters/exchanges/adapters/lighter_websocket.py`）。

一个可以落地的“定量结论”是：  
- **真实 orderbook 更新频率 = 运行时观测值（WS 推送）**；  
- **UI 展示刷新频率 = 1Hz（固定）**；  
- **分析循环频率目标 = 100Hz（固定上限/目标，使用最新缓存做计算，不追求处理每一条历史消息）**。

**实测样本（ETH 订单簿推送量级）**  
为了回答“到底一秒几次”，我用本仓库的适配器对 **ETH** 订单簿做了一个短窗统计：对每个交易所订阅 orderbook，统计回调触发次数并换算为 `count / window_seconds`。该数字只代表“当前网络与当时市场活跃度下的观测值”，不保证恒定。

- 观测窗口：12 秒  
- 标准 symbol：`ETH-USDC-PERP`（内部会转换为各交易所格式）  
- 观测口径：orderbook 回调触发次数（≈ 订单簿更新条数）  
- 结果（约值）：
  - **EdgeX**：`ETHUSD` → ~`1.42 次/秒`（12s 内 17 次）
  - **Lighter**：`ETH` → ~`13.58 次/秒`（12s 内 163 次）
  - **Paradex**：`ETH-USD-PERP` → ~`7.25 次/秒`（12s 内 87 次）

#### “即时/每秒接收多少”口径（运行时变量）
系统是事件驱动（WebSocket 推送），不是固定每秒轮询，因此“每秒接收多少”由交易所通道决定：
- **Lighter**：代码注释明确建议 `market_stats` 频道获取实时价格，约 **13 次/秒**（见 `core/adapters/exchanges/adapters/lighter_websocket.py` 头部维护说明）
- **Paradex / EdgeX**：代码未写死固定 Hz；可通过日志/统计观测：
  - `DataReceiver.stats['orderbook_received'/'ticker_received']` 反映入队量
  - `DataProcessor` 的滑动窗口统计与队列峰值反映压力与积压

#### “忽视不做 WebSocket 的币种”如何处理（确定行为）
以当前 `config/arbitrage/monitor_v2.yaml` 为例：**43 个候选币种处于注释状态**，系统行为是：
- **不会订阅（核心点）**：因为注释行压根不在 `symbols` 列表里，所以不会对它们调用任何 `subscribe_*` / `batch_subscribe_*`。
- **不会降频（不是“少看点”）**：这里不存在“把 43 个币种降频订阅”的逻辑；它们是“彻底不看”（0 条 WS 通道）。
- 防御性忽略（少数交易所可能推送杂项/映射异常时兜底）：接收回调会先做 `if std_symbol in symbols` 检查，不匹配则直接忽略（不入队），避免“订阅范围外的数据”污染缓存（见 `core/services/arbitrage_monitor_v2/data/data_receiver.py`）。

#### 队列满/高压时，是丢弃还是降频？（确定行为）
高压时采取“保最新、丢旧”的策略（属于隐式降采样，但不是主动降低订阅频率）：
- 先区分两个概念：
  - **订阅降频**：告诉交易所“少发点”，或者客户端主动“少订阅/少处理某些 symbol”（当前实现没有做）。
  - **丢旧保新**：交易所照常高速推送，但本机如果处理不过来，会把“排队太久的旧消息”丢掉，只保留最新的快照（当前实现就是这个）。
- 接收层（入队阶段）：队列满时会先 `get_nowait()` 丢掉一个最旧元素，再 `put_nowait()` 写入最新（`core/services/arbitrage_monitor_v2/data/data_receiver.py:DataReceiver._put_latest`）。
- 处理层（消费阶段）：队列接近满（≥80%）时，先丢弃一些最旧元素，把队列压力降下来，然后在 5ms 时间片内尽可能多处理最新数据（`core/services/arbitrage_monitor_v2/data/data_processor.py:DataProcessor._drain_queue`）。
- 直观理解：如果某个订单簿 1 秒推 200 条，而你机器只能处理 50 条，那么系统会倾向于“每秒拿到最新的 50 条快照”，而不是“慢慢把 200 条都处理完导致 UI 落后好几秒/几十秒”。

#### “每个交易所涵盖多少合约”的两个口径（当前可得数据）
建议在文档与监控指标中区分两种含义：
1) **本系统“订阅覆盖”的合约数**（确定）：等于 `symbols` 的数量（当前每个交易所 4 个）。  
2) **交易所“全市场可用”的合约/市场数量**（运行时可变；本仓库有静态快照可参考）：
- Lighter 市场精度快照：`config/exchanges/lighter_markets.json` 记录 **118** 个基础 token 的精度信息（price_decimals/size_decimals）
- EdgeX 市场快照：`config/exchanges/edgex_markets.json` 记录 **232** 个市场（示例：`BTCUSD/ETHUSD/...`）
- EdgeX×Lighter 重叠快照：`config/exchanges/edgex_lighter_markets.json`
  - `edgex_total=232`
  - `lighter_total=105`
  - `total_overlapping_symbols=79`
- Paradex：仓库内未提供静态 markets 列表；运行时可通过 REST `/markets` 获取并统计（`core/adapters/exchanges/adapters/paradex_rest.py`）

### 是否会触发 REST API 418？
- 现状：监控链路**主要依赖 WebSocket** 获取订单簿/行情；REST 多用于元数据、余额/持仓、少量兜底查询。
- 限流/避让：当前集中处理 **429/Too Many Requests**，以及特定交易所的 **21104 invalid nonce**：
  - `core/services/arbitrage_monitor_v2/risk_control/error_backoff_controller.py`
  - 没有对 **418** 做显式识别/专门避让策略。
- 结论：
  - 418 在不同交易所含义不一（有些用作封禁/反爬信号），本代码**不会“主动触发 418”**，但如果某交易所确实返回 418，当前多半会作为普通异常记录/抛出，**不会自动进入 418 专属退避**（属于可增强点）。

### 能否成为服务供其它仓库调用？
可以。现有实现已经把“采集/缓存/分析/UI”拆分为相对清晰的模块边界，具备服务化的工程基础。

推荐两条路线：
- **同进程复用（Python 项目最省事）**
  - 其它仓库把本仓库作为依赖（例如 editable 安装），直接 import `ArbitrageOrchestrator` 启动并读取内存态（stats/opportunities/orderbooks）。
- **独立进程微服务（跨仓库/跨语言更稳）**
  - 新增一个 API 层（例如 FastAPI/gRPC/WebSocket）：
    - 后台运行 orchestrator（无 UI）
    - 提供 `GET /snapshot`（stats + opportunities + price table 原始数据）
    - 提供 `WS /stream` 推送增量（ticker/orderbook/opportunity 更新）

## 代码地图（关键文件）
### 入口与配置
- `run_arbitrage_monitor_v2.py`
- `config/arbitrage/monitor_v2.yaml`

### 调度与核心循环
- `core/services/arbitrage_monitor_v2/core/orchestrator.py`
  - `start()`：连接适配器、订阅、启动各模块
  - `_analysis_loop()`：拉取缓存→计算→过滤→推 UI
  - `_update_ui()`：UI 数据采样/节流

### 数据层
- `core/services/arbitrage_monitor_v2/data/data_receiver.py`（WS 回调 → 入队）
- `core/services/arbitrage_monitor_v2/data/data_processor.py`（队列 drain → 最新缓存）
- `core/services/arbitrage_monitor/utils/symbol_converter.py`（统一符号转换）

### 分析层
- `core/services/arbitrage_monitor_v2/analysis/spread_calculator.py`
- `core/services/arbitrage_monitor_v2/analysis/opportunity_finder.py`

### UI 层（Rich）
- `core/services/arbitrage_monitor_v2/display/ui_manager.py`
- `core/services/arbitrage_monitor_v2/display/ui_components.py`

### 交易所适配器层（节选）
- `core/adapters/exchanges/factory.py`
- `core/adapters/exchanges/adapters/lighter_websocket.py`
- `core/adapters/exchanges/adapters/paradex_rest.py`
- `core/adapters/exchanges/adapters/edgex_websocket.py`

### 风险/避让（与监控相关）
- `core/services/arbitrage_monitor_v2/risk_control/error_backoff_controller.py`（429/21104 避让）

## 监控模式的关键策略（性能与稳定性）
- **队列满丢旧保新**：优先实时性而非全量回放，避免 UI/分析被积压拖死。
- **时间片 drain**：在固定时间预算内尽可能多消费队列，降低延迟尖峰。
- **UI 重数据采样/节流**：减少“每次 UI tick 都全量扫所有 symbol × exchange”的成本。
- **日志不刷屏**：多个模块将 console 输出关闭，仅落盘文件，避免终端 UI 抖动。

## 服务化改造计划（可执行）
### Phase 1：补齐无 UI 模式（Headless）
- 让 `--no-ui` 真正生效：不启动 Rich Live，仅后台采集/分析，提供程序内接口读取快照。
- 输出稳定的结构化 snapshot（Python dict / JSON 序列化友好）。

### Phase 2：对外 API（HTTP/WS）
- 新增 `monitor_service.py`（建议 FastAPI）：
  - `GET /health`：运行状态、订阅数、延迟指标
  - `GET /snapshot`：stats + opportunities + 各交易所 BBO/资金费率
  - `WS /stream`：推送机会变化与关键行情变更

### Phase 3：运行与部署
- 作为 systemd/docker 服务运行，外部系统通过 HTTP/WS 拉取数据。
- 将敏感配置（keys、jwt）全部走环境变量或挂载配置文件。

## 风险与待增强点
- **418 未被识别为避让类型**：若目标交易所使用 418 表示封禁/反爬，建议纳入错误分类并触发退避（与 429 同级或更高等级）。
- **REST 异常统一化**：不同适配器对非 2xx 的处理风格不完全一致，服务化后建议统一错误结构与重试/退避策略。
