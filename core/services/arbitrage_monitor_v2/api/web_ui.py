from __future__ import annotations


def render_monitor_ui_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Monitor/V2 - Spread Dashboard</title>
    <style>
      :root {
        --bg: #0b1020;
        --panel: #111a33;
        --text: #e6e9f2;
        --muted: #9aa3b2;
        --good: #30d158;
        --bad: #ff453a;
        --warn: #ffd60a;
        --border: rgba(255,255,255,.08);
        --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji", "Segoe UI Emoji";
      }
      body { margin: 0; font-family: var(--sans); background: var(--bg); color: var(--text); }
      header { position: sticky; top: 0; z-index: 10; background: rgba(11,16,32,.95); backdrop-filter: blur(8px); border-bottom: 1px solid var(--border); }
      .wrap { max-width: 1400px; margin: 0 auto; padding: 12px 14px; }
      h1 { margin: 6px 0 10px; font-size: 16px; font-weight: 650; letter-spacing: .2px; }
      .row { display: grid; grid-template-columns: 1.2fr 2fr; gap: 10px; align-items: start; }
      .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 12px; }
      .grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; }
      .k { color: var(--muted); font-size: 12px; }
      .v { font-family: var(--mono); font-size: 12px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .controls { display: grid; grid-template-columns: 1.5fr 1fr 1fr 1fr; gap: 10px; }
      input, select, button {
        background: rgba(255,255,255,.03);
        color: var(--text);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 10px 10px;
        font-size: 12px;
        outline: none;
      }
      button { cursor: pointer; }
      button:hover { border-color: rgba(255,255,255,.18); }
      .statusDot { display:inline-block; width: 10px; height:10px; border-radius: 999px; margin-right: 8px; background: var(--warn); vertical-align: middle; }
      .statusText { font-family: var(--mono); font-size: 12px; color: var(--muted); }
      .tables { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
      table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12px; }
      th, td { padding: 8px 8px; border-bottom: 1px solid var(--border); }
      th { text-align: left; color: var(--muted); font-weight: 600; position: sticky; top: 58px; background: var(--panel); }
      td.r { text-align: right; }
      .pct.pos { color: var(--good); }
      .pct.neg { color: var(--bad); }
      .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); font-size: 11px; font-family: var(--mono); }
      .small { font-size: 11px; color: var(--muted); font-family: var(--mono); }
      .footer { margin-top: 10px; color: var(--muted); font-family: var(--mono); font-size: 11px; }
      .watchlist { display: grid; grid-template-columns: 1fr 2fr; gap: 10px; margin-top: 10px; }
      .watchlistBox { border: 1px solid var(--border); border-radius: 10px; padding: 8px; background: rgba(255,255,255,.02); }
      .taglist { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
      .tag { display: inline-flex; align-items: center; border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 11px; font-family: var(--mono); color: var(--text); }
      @media (max-width: 980px) {
        .row { grid-template-columns: 1fr; }
        .tables { grid-template-columns: 1fr; }
        .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .controls { grid-template-columns: 1fr 1fr; }
        .watchlist { grid-template-columns: 1fr; }
        th { top: 104px; }
      }
    </style>
  </head>
  <body>
    <header>
      <div class="wrap">
        <h1>
          <span id="statusDot" class="statusDot"></span>
          Monitor/V2 Spread Dashboard
          <span class="pill" id="connState">connecting</span>
          <span class="small" id="clock"></span>
        </h1>
        <div class="panel">
          <div class="grid">
            <div><div class="k">analysis_age</div><div class="v" id="analysisAge">-</div></div>
            <div><div class="k">last_analysis_at</div><div class="v" id="lastAnalysisAt">-</div></div>
            <div><div class="k">watchlist_pairs</div><div class="v" id="watchPairs">-</div></div>
            <div><div class="k">symbols_with_spreads</div><div class="v" id="symbolsWithSpreads">-</div></div>
            <div><div class="k">opportunities</div><div class="v" id="oppsCount">-</div></div>
            <div><div class="k">queue/backpressure</div><div class="v" id="queueInfo">-</div></div>
          </div>
          <div style="height:10px"></div>
          <div class="controls">
            <input id="symbolFilter" placeholder="过滤 symbol（例：BTC / ETH-USDC-PERP）" />
            <input id="minAbsPct" placeholder="min |spread_pct|（例：0.20）" />
            <select id="intervalMs">
              <option value="250">250ms</option>
              <option value="500">500ms</option>
              <option value="1000" selected>1000ms</option>
              <option value="2000">2000ms</option>
            </select>
            <button id="applyBtn">应用 / 重连</button>
          </div>
          <div class="watchlist">
            <div class="watchlistBox">
              <div class="k">当前关注的交易所</div>
              <div class="taglist" id="watchExchanges"></div>
            </div>
            <div class="watchlistBox">
              <div class="k">当前关注的币种清单</div>
              <div class="taglist" id="watchSymbols"></div>
            </div>
          </div>
        </div>
      </div>
    </header>

    <main class="wrap">
      <div class="tables">
        <div class="panel">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
            <div>
              <div style="font-weight:650; margin-bottom:2px;">Opportunities</div>
              <div class="small">来自机会识别器（通常已过滤阈值）</div>
            </div>
            <div class="pill" id="oppsHint">top 50</div>
          </div>
          <div style="height:10px"></div>
          <div style="max-height: 62vh; overflow:auto;">
            <table>
              <thead>
                <tr>
                  <th>symbol</th>
                  <th>buy</th>
                  <th>sell</th>
                  <th class="r">buy_px</th>
                  <th class="r">sell_px</th>
                  <th class="r">spread</th>
                </tr>
              </thead>
              <tbody id="oppsBody"></tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:10px;">
            <div>
              <div style="font-weight:650; margin-bottom:2px;">Spreads</div>
              <div class="small">全量价差里按 |spread_pct| 选取 top N（低频推送，避免性能开销）</div>
            </div>
            <div class="pill" id="spreadsHint">top 200</div>
          </div>
          <div style="height:10px"></div>
          <div style="max-height: 62vh; overflow:auto;">
            <table>
              <thead>
                <tr>
                  <th>symbol</th>
                  <th>buy</th>
                  <th>sell</th>
                  <th class="r">buy_px</th>
                  <th class="r">sell_px</th>
                  <th class="r">spread</th>
                </tr>
              </thead>
              <tbody id="spreadsBody"></tbody>
            </table>
          </div>
        </div>
      </div>
      <div class="footer">
        仅使用 HTTP 轮询（`/ui/data`），不依赖 WebSocket。
      </div>
    </main>

    <script>
      const el = (id) => document.getElementById(id);
      const fmt = {
        pct: (v) => {
          if (v === null || v === undefined || Number.isNaN(v)) return "-";
          const s = (v >= 0 ? "+" : "") + v.toFixed(3) + "%";
          return s;
        },
        num: (v) => {
          if (v === null || v === undefined || Number.isNaN(v)) return "-";
          if (Math.abs(v) >= 1000) return v.toFixed(2);
          if (Math.abs(v) >= 10) return v.toFixed(4);
          return v.toFixed(6);
        },
        ms: (v) => {
          if (v === null || v === undefined || Number.isNaN(v)) return "-";
          if (v < 1000) return v.toFixed(0) + "ms";
          return (v / 1000).toFixed(2) + "s";
        }
      };
      const esc = (v) => String(v).replace(/[&<>"']/g, (m) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      })[m]);

      let pollTimer = null;

      function buildQuery() {
        const intervalMs = parseInt(el("intervalMs").value || "1000", 10);
        const sym = (el("symbolFilter").value || "").trim();
        const minAbs = (el("minAbsPct").value || "").trim();

        const qs = new URLSearchParams();
        qs.set("interval_ms", String(intervalMs));
        qs.set("top_spreads", "200");
        qs.set("top_opps", "50");
        if (sym.length) qs.set("symbol_like", sym);
        if (minAbs.length) qs.set("min_abs_spread_pct", minAbs);
        return { intervalMs, qs };
      }

      function buildHttpUrl() {
        const { qs } = buildQuery();
        return `${location.origin}/ui/data?${qs.toString()}`;
      }

      function setConnState(state, ok) {
        el("connState").textContent = state;
        el("statusDot").style.background = ok ? "var(--good)" : "var(--warn)";
      }

      function stopPolling() {
        if (pollTimer) {
          clearTimeout(pollTimer);
          pollTimer = null;
        }
      }

      function renderRows(tbody, rows) {
        const html = (rows || []).map(r => {
          const pct = Number(r.spread_pct);
          const cls = (pct >= 0) ? "pos" : "neg";
          return `<tr>
            <td>${r.symbol || "-"}</td>
            <td>${r.exchange_buy || "-"}</td>
            <td>${r.exchange_sell || "-"}</td>
            <td class="r">${fmt.num(Number(r.price_buy))}</td>
            <td class="r">${fmt.num(Number(r.price_sell))}</td>
            <td class="r pct ${cls}">${fmt.pct(pct)}</td>
          </tr>`;
        }).join("");
        tbody.innerHTML = html;
      }

      function renderTags(target, items) {
        if (!items || items.length === 0) {
          target.innerHTML = `<span class="small">-</span>`;
          return;
        }
        target.innerHTML = items.map(i => `<span class="tag">${esc(i)}</span>`).join("");
      }

      function applyPayload(p) {
        const now = Date.now();
        el("clock").textContent = "  " + new Date(now).toLocaleString();

        const analysisAgeMs = (p.analysis_age_ms === null || p.analysis_age_ms === undefined) ? null : Number(p.analysis_age_ms);
        el("analysisAge").textContent = fmt.ms(analysisAgeMs);
        el("lastAnalysisAt").textContent = (p.last_analysis_at ? new Date(Number(p.last_analysis_at) * 1000).toLocaleTimeString() : "-");
        el("watchPairs").textContent = String(p.watchlist_pairs ?? "-");
        el("symbolsWithSpreads").textContent = String(p.symbols_with_spreads ?? "-");
        el("oppsCount").textContent = String(p.opportunities_count ?? "-");
        el("queueInfo").textContent = p.queue_info || "-";

        el("oppsHint").textContent = `top ${p.top_opps || 0}`;
        el("spreadsHint").textContent = `top ${p.top_spreads || 0}`;

        renderRows(el("oppsBody"), p.opportunities || []);
        renderRows(el("spreadsBody"), p.top_spread_rows || []);
        renderTags(el("watchExchanges"), p.watchlist_exchanges || []);
        renderTags(el("watchSymbols"), p.watchlist_symbols || []);
      }

      async function fetchSnapshotOnce() {
        const url = buildHttpUrl();
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) throw new Error(`http ${r.status}`);
        const p = await r.json();
        if (p && p.type === "snapshot") applyPayload(p);
      }

      function startPolling() {
        stopPolling();
        setConnState("polling", true);
        const { intervalMs } = buildQuery();

        const loop = async () => {
          if (document.hidden) {
            pollTimer = setTimeout(loop, intervalMs);
            return;
          }
          try {
            await fetchSnapshotOnce();
          } catch (_) {}
          pollTimer = setTimeout(loop, intervalMs);
        };
        loop();
      }

      el("applyBtn").addEventListener("click", () => startPolling());
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) {
          stopPolling();
          setConnState("paused", false);
        } else {
          startPolling();
        }
      });
      startPolling();
    </script>
  </body>
</html>
"""
