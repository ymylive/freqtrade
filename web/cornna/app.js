const utcEl = document.getElementById("utc-time");
const langToggle = document.getElementById("lang-toggle");

const translations = {
  nav_signal: { en: "Signal", zh: "信号" },
  nav_pipeline: { en: "Pipeline", zh: "流程" },
  nav_monitor: { en: "AI Monitor", zh: "AI 监控" },
  nav_insight: { en: "Insight", zh: "洞察" },
  nav_contact: { en: "Contact", zh: "联系" },
  cta_request: { en: "Request Access", zh: "申请访问" },
  hero_eyebrow: { en: "ValueScan Intelligence / AI-Tuned Execution", zh: "ValueScan 智能 / AI 微调执行" },
  hero_title_a: { en: "Quant signals shaped by live flow,", zh: "量化信号由实时资金流塑形，" },
  hero_title_b: { en: "calibrated in real time.", zh: "并实时校准。" },
  hero_lead: {
    en: "Cornna distills ValueScan inflows, sentiment, and whale activity into a compact decision layer. No clutter, only signals that matter.",
    zh: "Cornna 将 ValueScan 的资金流、情绪与鲸鱼活动收敛为精简决策层。去除噪声，只保留关键信号。",
  },
  cta_console: { en: "Open Console", zh: "打开控制台" },
  metric_flow_bias: { en: "Flow Bias", zh: "资金倾向" },
  metric_flow_hint: { en: "Net inflow 24h", zh: "24h 净流入" },
  metric_whale_pressure: { en: "Whale Pressure", zh: "鲸鱼压力" },
  metric_whale_hint: { en: "Long leaning", zh: "偏多" },
  metric_sentiment: { en: "Sentiment", zh: "情绪" },
  metric_sentiment_hint: { en: "AI bullish ratio", zh: "AI 多头占比" },
  panel_signal_title: { en: "Signal Summary", zh: "信号摘要" },
  panel_signal_body: {
    en: "Compact scoring across flow, volatility, and sentiment. Thresholds self-adjust to trading outcomes.",
    zh: "资金流、波动率与情绪的紧凑评分。阈值随交易结果自动校准。",
  },
  pill_flow: { en: "Flow", zh: "资金流" },
  pill_sentiment: { en: "Sentiment", zh: "情绪" },
  pill_volatility: { en: "Volatility", zh: "波动率" },
  pill_risk: { en: "Risk", zh: "风险" },
  panel_exec_title: { en: "Execution Logic", zh: "执行逻辑" },
  panel_exec_body: {
    en: "Simple decision tree. Real-time ValueScan data only. No historic backtest assumptions.",
    zh: "简洁决策树，仅使用实时 ValueScan 数据，不依赖历史回测假设。",
  },
  exec_entry: { en: "Entry", zh: "入场" },
  exec_entry_desc: { en: "Signal strength + flow lift", zh: "信号强度 + 资金流抬升" },
  exec_exit: { en: "Exit", zh: "离场" },
  exec_exit_desc: { en: "Flow fade + sentiment reversal", zh: "资金流衰减 + 情绪反转" },
  exec_risk: { en: "Risk", zh: "风险" },
  exec_risk_desc: { en: "ATR-driven exposure clamp", zh: "ATR 驱动的仓位约束" },
  panel_feedback_title: { en: "Live Feedback", zh: "实时反馈" },
  panel_feedback_body: {
    en: "Every trade feeds the tuner. Edges are reinforced, noise is trimmed.",
    zh: "每笔交易回流到调参器，强化优势，削弱噪声。",
  },
  pipeline_title: { en: "Pipeline", zh: "流程" },
  pipeline_body: { en: "Three stages. One clean loop. Designed for rapid decisions.", zh: "三段流程，闭环执行，专为快速决策。" },
  pipeline_ingest_title: { en: "Ingest", zh: "采集" },
  pipeline_ingest_body: { en: "ValueScan flows, whale heat, AI ratios.", zh: "ValueScan 资金流、鲸鱼热度与 AI 比例。" },
  pipeline_score_title: { en: "Score", zh: "评分" },
  pipeline_score_body: { en: "Normalize, weight, and compute strategy bias.", zh: "归一化、加权并计算策略倾向。" },
  pipeline_exec_title: { en: "Execute", zh: "执行" },
  pipeline_exec_body: { en: "Run tuned thresholds. Record outcomes.", zh: "运行微调阈值，记录结果。" },
  monitor_title: { en: "AI Monitor", zh: "AI 监控" },
  monitor_body: {
    en: "Track live signal cadence, model drift, and operator alerts in a single console view.",
    zh: "在一个控制台中追踪信号频率、模型漂移与操作告警。",
  },
  monitor_backend: { en: "Backend", zh: "后端" },
  monitor_status_title: { en: "Signal Health", zh: "信号健康度" },
  monitor_status_label: { en: "Active Feed", zh: "活动推送" },
  monitor_refresh_label: { en: "Refresh Window", zh: "刷新窗口" },
  monitor_alerts_title: { en: "Live Alerts", zh: "实时告警" },
  monitor_alert_1: { en: "Whale inflow spike detected", zh: "检测到鲸鱼资金流入激增" },
  monitor_alert_2: { en: "Sentiment delta crosses threshold", zh: "情绪变化突破阈值" },
  monitor_alert_3: { en: "Flow regime shifted to positive", zh: "资金流状态转为正向" },
  monitor_feedback_title: { en: "AI Feedback Loop", zh: "AI 反馈链路" },
  monitor_feedback_label: { en: "Tuned parameters", zh: "已微调参数" },
  monitor_feedback_trades: { en: "Trades evaluated", zh: "评估交易数" },
  insight_title: { en: "Market Insight", zh: "市场洞察" },
  insight_body: {
    en: "A single view of inflow momentum, sentiment delta, and exchange net flow. Keep the surface area minimal, stay in control.",
    zh: "单页呈现资金流动量、情绪变化与交易所净流。界面极简，掌控全局。",
  },
  cta_snapshot: { en: "Download Snapshot", zh: "下载快照" },
  netflow_btc: { en: "Net Flow +128M", zh: "净流入 +128M" },
  netflow_eth: { en: "Net Flow +84M", zh: "净流入 +84M" },
  netflow_sol: { en: "Net Flow -22M", zh: "净流入 -22M" },
  netflow_arb: { en: "Net Flow +9M", zh: "净流入 +9M" },
  footer_title: { en: "Cornna Quant Console", zh: "Cornna 量化控制台" },
  footer_body: { en: "Signal clarity for live ValueScan intelligence.", zh: "为实时 ValueScan 智能提供清晰信号。" },
  footer_status: { en: "Status", zh: "状态" },
  footer_docs: { en: "Docs", zh: "文档" },
  status_online: { en: "Live", zh: "在线" },
  status_offline: { en: "Offline", zh: "离线" },
};

const pad = (value) => String(value).padStart(2, "0");

const updateUtc = () => {
  if (!utcEl) return;
  const now = new Date();
  const hh = pad(now.getUTCHours());
  const mm = pad(now.getUTCMinutes());
  const ss = pad(now.getUTCSeconds());
  utcEl.textContent = `${hh}:${mm}:${ss}`;
};

let backendOnline = false;

const applyLanguage = (lang) => {
  document.documentElement.lang = lang;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const key = node.dataset.i18n;
    const entry = translations[key];
    if (entry) {
      node.textContent = entry[lang] || entry.en;
    }
  });
  if (langToggle) {
    langToggle.textContent = lang === "en" ? "中文" : "EN";
  }
  setBackendStatus(backendOnline);
};

const initLanguage = () => {
  let stored = "en";
  try {
    stored = localStorage.getItem("cornna_lang") || "en";
  } catch (err) {
    stored = document.documentElement.lang || "en";
  }
  const lang = stored === "zh" ? "zh" : "en";
  applyLanguage(lang);
};

if (langToggle) {
  langToggle.addEventListener("click", () => {
    const current = document.documentElement.lang === "zh" ? "zh" : "en";
    const next = current === "zh" ? "en" : "zh";
    try {
      localStorage.setItem("cornna_lang", next);
    } catch (err) {
      // Ignore storage errors (private mode or blocked storage)
    }
    applyLanguage(next);
  });
}

const apiBase = window.CORNNA_API_BASE || "";
const monitorPath = window.CORNNA_API_MONITOR || "/api/monitor";
const monitorUrl = `${apiBase}${monitorPath}`;

const metricNodes = {
  flow_bias: document.querySelector('[data-metric="flow_bias"]'),
  whale_pressure: document.querySelector('[data-metric="whale_pressure"]'),
  sentiment: document.querySelector('[data-metric="sentiment"]'),
  signal_health: document.querySelector('[data-metric="signal_health"]'),
  refresh_window: document.querySelector('[data-metric="refresh_window"]'),
  tuned_params: document.querySelector('[data-metric="tuned_params"]'),
  trades_evaluated: document.querySelector('[data-metric="trades_evaluated"]'),
  alerts: document.querySelector('[data-metric="alerts"]'),
  backend_status: document.querySelector('[data-metric="backend_status"]'),
};

const numberOrNull = (value) => {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
};

const formatPercent = (value, fallback = "--") => {
  const num = numberOrNull(value);
  if (num === null) return fallback;
  const finalValue = num > 1 ? num : num * 100;
  return `${finalValue.toFixed(1)}%`;
};

const formatSigned = (value, fallback = "--") => {
  const num = numberOrNull(value);
  if (num === null) return fallback;
  const sign = num > 0 ? "+" : "";
  return `${sign}${num.toFixed(2)}`;
};

const formatInt = (value, fallback = "--") => {
  const num = numberOrNull(value);
  if (num === null) return fallback;
  return `${Math.round(num)}`;
};

const formatWindow = (value, fallback = "--") => {
  const num = numberOrNull(value);
  if (num === null) return fallback;
  return `${Math.round(num)}s`;
};

const setBackendStatus = (isOnline) => {
  backendOnline = isOnline;
  const statusNode = metricNodes.backend_status;
  if (!statusNode) return;
  const label = statusNode.querySelector("strong");
  const lang = document.documentElement.lang === "zh" ? "zh" : "en";
  if (label) {
    label.textContent = isOnline
      ? translations.status_online[lang]
      : translations.status_offline[lang];
  }
  statusNode.classList.toggle("is-offline", !isOnline);
};

const updateAlerts = (alerts) => {
  if (!metricNodes.alerts || !Array.isArray(alerts) || alerts.length === 0) return;
  metricNodes.alerts.innerHTML = "";
  alerts.slice(0, 3).forEach((alert) => {
    const item = document.createElement("li");
    item.textContent = String(alert);
    metricNodes.alerts.appendChild(item);
  });
};

const applyMonitorData = (payload) => {
  if (!payload || typeof payload !== "object") return;
  if (metricNodes.flow_bias) metricNodes.flow_bias.textContent = formatSigned(payload.flow_bias);
  if (metricNodes.whale_pressure) metricNodes.whale_pressure.textContent = formatPercent(payload.whale_pressure);
  if (metricNodes.sentiment) metricNodes.sentiment.textContent = formatInt(payload.sentiment);
  if (metricNodes.signal_health) metricNodes.signal_health.textContent = formatPercent(payload.signal_health);
  if (metricNodes.refresh_window) metricNodes.refresh_window.textContent = formatWindow(payload.refresh_window);
  if (metricNodes.tuned_params) metricNodes.tuned_params.textContent = formatInt(payload.tuned_params);
  if (metricNodes.trades_evaluated) metricNodes.trades_evaluated.textContent = formatInt(payload.trades_evaluated);
  if (payload.alerts) updateAlerts(payload.alerts);
};

const fetchMonitor = async () => {
  if (!monitorUrl) return;
  try {
    const response = await fetch(monitorUrl, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    applyMonitorData(data);
    setBackendStatus(true);
  } catch (err) {
    setBackendStatus(false);
  }
};

const boot = () => {
  updateUtc();
  setInterval(updateUtc, 1000);
  initLanguage();
  fetchMonitor();
  setInterval(fetchMonitor, 15000);
};

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
