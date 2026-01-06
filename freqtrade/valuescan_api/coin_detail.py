#!/usr/bin/env python3
# ruff: noqa: RUF002, E501, S110, S112, C901
"""
ValuScan 币种详情查询模块
提供简洁的接口供其他组件通过币种名称获取详细数据
"""
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any


# 导入客户端
if TYPE_CHECKING:
    from .client import ValuScanClient
else:
    try:
        from .client import ValuScanClient
    except ImportError:
        from client import ValuScanClient


# 全局客户端实例
_client: ValuScanClient | None = None

# 币种名称到 keyword 的映射缓存
_symbol_cache: dict[str, int] = {}

BASE_DIR = Path(__file__).resolve().parent.parent
LOCALSTORAGE_FILE = Path(
    os.getenv("VALUESCAN_LOCALSTORAGE_FILE")
    or BASE_DIR / "signal_monitor" / "valuescan_localstorage.json"
)
DEFAULT_FUND_FLOW_TAGS = [
    "m5",
    "m15",
    "m30",
    "H1",
    "H4",
    "H12",
    "H24",
    "D3",
    "D7",
    "D15",
    "D30",
]
DEFAULT_VOLUME_TAGS = [
    "m5",
    "m15",
    "m30",
    "H1",
    "H4",
    "H12",
    "H24",
    "D3",
    "D7",
    "D15",
    "D30",
]
DEFAULT_WHALE_FLOW_TAGS = [
    "m5",
    "m15",
    "m30",
    "H1",
    "H4",
    "H8",
    "H12",
    "H24",
    "D3",
    "D7",
    "D15",
    "D30",
]
DEFAULT_HEATMAP_INTERVALS = ["1h", "4h", "12h", "1d"]


def _parse_tag_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                items = json.loads(raw)
                if isinstance(items, list):
                    return [str(item).strip() for item in items if str(item).strip()]
            except Exception:
                pass
        return [raw]
    return []


def _load_localstorage_tags(key: str, fallback: list[str]) -> list[str]:
    try:
        if LOCALSTORAGE_FILE.exists():
            payload = json.loads(LOCALSTORAGE_FILE.read_text(encoding="utf-8"))
            tags = _parse_tag_list(payload.get(key))
            if tags:
                return tags
    except Exception:
        pass
    return list(fallback)


def _load_localstorage_value(key: str, default: str | None) -> str | None:
    try:
        if LOCALSTORAGE_FILE.exists():
            payload = json.loads(LOCALSTORAGE_FILE.read_text(encoding="utf-8"))
            value = payload.get(key)
            if value is None:
                return default
            text = str(value).strip()
            return text if text else default
    except Exception:
        pass
    return default


def _normalize_flow_period(value: Any) -> str:
    if value is None:
        return ""
    key = str(value).strip().lower().replace(" ", "")
    aliases = {
        "h1": "1h",
        "h4": "4h",
        "h12": "12h",
        "h24": "24h",
        "1d": "24h",
        "d1": "24h",
        "d": "24h",
        "m15": "15m",
    }
    return aliases.get(key, key)


def _first_float(item: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _extract_flow_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "items"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        fallback_items: list[dict[str, Any]] = []
        for key, value in data.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("timeType", key)
                fallback_items.append(item)
        return fallback_items
    return []


def _normalize_exchange_flow_detail(resp: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    if not isinstance(resp, dict) or resp.get("code") != 200:
        return {}
    items = _extract_flow_items(resp.get("data"))
    result: dict[str, dict[str, float]] = {}
    for item in items:
        period = _normalize_flow_period(
            item.get("timeType")
            or item.get("period")
            or item.get("time")
            or item.get("timeParticle")
        )
        if not period:
            continue
        in_val = _first_float(item, ["inFlowValue", "inFlow", "tradeIn", "stopTradeIn", "contractTradeIn"])
        out_val = _first_float(item, ["outFlowValue", "outFlow", "tradeOut", "stopTradeOut", "contractTradeOut"])
        net_val = _first_float(
            item,
            ["netFlowValue", "netFlow", "tradeInflow", "stopTradeInflow", "contractTradeInflow"],
        )
        if net_val is None and in_val is not None and out_val is not None:
            net_val = in_val - out_val
        if in_val is None and out_val is None and net_val is None:
            continue
        total = (in_val or 0.0) + (out_val or 0.0)
        ratio = (in_val or 0.0) / total if total > 0 else 0.5
        result[period] = {
            "in": float(in_val or 0.0),
            "out": float(out_val or 0.0),
            "net": float(net_val or 0.0),
            "ratio": float(ratio),
        }
    return result


def _extract_valuescan_list(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "items", "data"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def _extract_max_positive_inflow(
    flow_by_time: Any,
    only_period: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(flow_by_time, dict):
        return None
    best: dict[str, Any] | None = None
    for period, data in flow_by_time.items():
        if only_period and period != only_period:
            continue
        items = _extract_valuescan_list({"data": data})
        for item in items:
            value = _first_float(
                item,
                ["inFlowValue", "tradeInflow", "netFlowValue", "inFlow", "tradeIn"],
            )
            if value is None or value <= 0:
                continue
            symbol = (
                item.get("symbol")
                or item.get("tokenSymbol")
                or item.get("name")
                or item.get("coinName")
            )
            if not symbol:
                continue
            if best is None or value > best["value"]:
                best = {"symbol": symbol, "time": period, "value": float(value)}
    return best


def _extract_dense_points(resp: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(resp, dict):
        return []
    data = resp.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "records", "items"):
            if isinstance(data.get(key), list):
                return data.get(key) or []
    return []


def _point_time_ms(point: dict[str, Any]) -> int:
    for key in (
        "time",
        "ts",
        "timestamp",
        "dateTime",
        "date",
        "createTime",
        "createdTime",
        "create_time",
        "created_at",
    ):
        value = point.get(key) if isinstance(point, dict) else None
        if value is None:
            continue
        try:
            ts = int(float(value))
        except Exception:
            continue
        if ts <= 0:
            continue
        return ts if ts > 10**12 else ts * 1000
    return 0


def _filter_points_by_days(points: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    if days <= 0:
        return points
    cutoff_ms = int(time.time() * 1000) - days * 24 * 60 * 60 * 1000
    return [p for p in points if _point_time_ms(p) >= cutoff_ms]


def _format_trade_pairs(symbol: str) -> str:
    base = symbol.upper().replace("$", "").replace("USDT", "").strip()
    return f"{base}USDT" if base else symbol.upper().strip()



def _get_client() -> ValuScanClient:
    """获取客户端实例"""
    global _client
    if _client is None:
        _client = ValuScanClient()
    return _client


def _load_symbol_cache():
    """加载币种符号缓存"""
    global _symbol_cache
    if _symbol_cache:
        return

    # 尝试从缓存文件加载
    cache_file = Path(__file__).parent / "data" / "symbol_cache.json"
    if cache_file.exists():
        try:
            _symbol_cache = json.loads(cache_file.read_text(encoding="utf-8"))
            return
        except Exception:
            pass

    # 从 API 加载
    client = _get_client()
    page = 1
    while True:
        resp = client.list_all_coins(page=page, page_size=100)
        if resp.get("code") != 200:
            break

        coins = resp.get("data", {}).get("list", [])
        if not coins:
            break

        for coin in coins:
            symbol = (coin.get("symbol") or "").upper()
            keyword = coin.get("vsTokenId") or coin.get("keyword")
            if symbol and keyword:
                _symbol_cache[symbol] = int(keyword)

        total = resp.get("data", {}).get("total", 0)
        if len(_symbol_cache) >= total:
            break
        page += 1

    # 保存缓存
    cache_file.parent.mkdir(exist_ok=True)
    cache_file.write_text(json.dumps(_symbol_cache, ensure_ascii=False), encoding="utf-8")



def get_keyword(symbol: str) -> int | None:
    """???????? keyword (ID)."""
    _load_symbol_cache()
    symbol = symbol.upper().strip()

    # ?????
    if symbol in _symbol_cache:
        return _symbol_cache[symbol]

    client = _get_client()
    resp = client.search_keyword(symbol, page=1, page_size=20)
    if resp.get("code") == 200:
        data = resp.get("data") or {}
        items: list[dict[str, Any]] = []
        if isinstance(data, dict):
            raw_items = data.get("list") or data.get("records") or data.get("items") or []
            if isinstance(raw_items, list):
                items = [item for item in raw_items if isinstance(item, dict)]
        elif isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        for coin in items:
            symbol_val = (coin.get("symbol") or coin.get("tokenSymbol") or "").upper()
            if symbol_val == symbol:
                keyword = int(coin.get("vsTokenId") or coin.get("keyword") or 0)
                if keyword:
                    _symbol_cache[symbol] = keyword
                    return keyword

    # ?????????????
    resp = client._request("POST", "/api/vs-token/queryCoin", json_body={
        "search": symbol,
        "page": 1,
        "pageSize": 20
    })

    if resp.get("code") == 200:
        coins = resp.get("data", {}).get("list", [])
        for coin in coins:
            if (coin.get("symbol") or "").upper() == symbol:
                keyword = int(coin.get("vsTokenId") or coin.get("keyword") or 0)
                if keyword:
                    _symbol_cache[symbol] = keyword
                    return keyword

    return None


def get_detail(symbol: str) -> dict[str, Any]:
    """
    通过币种符号获取完整详情

    Args:
        symbol: 币种符号，如 "BTC", "ETH", "SOL"

    Returns:
        包含完整详情的字典，结构如下:
        {
            "code": 200,
            "symbol": "ETH",
            "keyword": 1027,
            "basic": {...},       # 基础信息
            "ai_summary": {...},  # AI分析摘要
            "trade_inflow": {...},# 资金流入数据
            "exchange_info": {...}# 交易所信息
        }
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    client = _get_client()
    result = client.get_coin_detail(keyword)

    if result.get("code") == 200:
        data = result.get("data", {})
        return {
            "code": 200,
            "symbol": symbol.upper(),
            "keyword": keyword,
            "basic": data.get("basic"),
            "ai_summary": data.get("ai_summary"),
            "trade_inflow": data.get("trade_inflow"),
            "exchange_info": data.get("exchange_info"),
            "exchange_flow_detail": data.get("exchange_flow_detail"),
            "fund_flow_history": data.get("fund_flow_history"),
            "fund_volume_history": data.get("fund_volume_history"),
            "holders_top": data.get("holders_top"),
            "chains": data.get("chains"),
        }

    return result


def get_basic(symbol: str) -> dict[str, Any]:
    """
    获取币种基础信息（价格、市值、涨跌幅等）

    Args:
        symbol: 币种符号

    Returns:
        基础信息字典
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    client = _get_client()
    resp = client._request("POST", "/api/vs-token/queryCoin", json_body={
        "search": symbol,
        "page": 1,
        "pageSize": 10
    })

    if resp.get("code") == 200:
        coins = resp.get("data", {}).get("list", [])
        for coin in coins:
            if (coin.get("symbol") or "").upper() == symbol.upper():
                return {"code": 200, "data": coin}

    return resp


def get_ai_analysis(symbol: str) -> dict[str, Any]:
    """
    获取币种 AI 分析摘要

    Args:
        symbol: 币种符号

    Returns:
        AI分析摘要，包含看涨/看跌/中立情绪比例和多语言分析
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    return _get_client().get_ai_summary(keyword)


def get_inflow(symbol: str) -> dict[str, Any]:
    """
    获取币种资金流入数据

    Args:
        symbol: 币种符号

    Returns:
        资金流入数据
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    return _get_client().get_trade_inflow(keyword)


def get_exchange_flow_detail(symbol: str) -> dict[str, Any]:
    """
    Get exchange flow detail (in/out/net) for multiple time ranges.
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}
    return _get_client().get_exchange_flow_detail(keyword)


def get_heat_map(interval: str = "1h") -> dict[str, Any]:
    """
    Get liquidation heat map data by interval (1h/4h/12h/1d).
    """
    return _get_client().get_heat_map(interval=interval)


def get_fund_trade_history_total(
    symbol: str,
    time_particle: str = "12h",
    limit_size: int = 100,
    flow: bool = True,
    trade_type: int | None = None,
) -> dict[str, Any]:
    """
    Get fund/volume history by time buckets.
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}
    return _get_client().get_fund_trade_history_total(
        keyword,
        time_particle=time_particle,
        limit_size=limit_size,
        flow=flow,
        trade_type=trade_type,
    )


def get_fund_trade_history_total_all(
    symbol: str,
    times: list[str] | None = None,
    limit_size: int = 60,
    flow: bool = True,
    trade_type: int | None = None,
) -> dict[str, Any]:
    if times is None:
        times = _load_localstorage_tags(
            "fund-flow-history-tags" if flow else "volume-history-tags",
            DEFAULT_FUND_FLOW_TAGS if flow else DEFAULT_VOLUME_TAGS,
        )
    data: dict[str, Any] = {}
    errors: dict[str, Any] = {}
    for period in times:
        payload = get_fund_trade_history_total(
            symbol,
            time_particle=period,
            limit_size=limit_size,
            flow=flow,
            trade_type=trade_type,
        )
        if isinstance(payload, dict) and payload.get("code") == 200:
            data[period] = payload.get("data")
        else:
            data[period] = None
            errors[period] = payload
    result = {"code": 200 if not errors else 500, "msg": "success" if not errors else "partial", "data": data}
    if errors:
        result["errors"] = errors
    return result


def get_holder_page(
    symbol: str,
    page: int = 1,
    page_size: int = 20,
    address: str = "",
    chain: str | None = None,
) -> dict[str, Any]:
    """
    Get top holders page for a coin.
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}
    return _get_client().get_holder_page(
        keyword,
        page=page,
        page_size=page_size,
        address=address,
        symbol=symbol,
        chain=chain,
    )


def get_chain_page(symbol: str = "", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """
    Get chain list for tokens (CMC chain page).
    """
    return _get_client().get_chain_page(symbol=symbol, page=page, page_size=page_size)


def get_kline_time() -> dict[str, Any]:
    """Get ValueScan kline time reference."""
    return _get_client().get_kline_time()


def get_trade_kline_history(
    symbol: str,
    kline_type: str = "01",
    bucket_type: str = "1s",
    size: int = 300,
) -> dict[str, Any]:
    """
    Get tradePairs kline history (ValueScan).
    """
    trade_pairs = _format_trade_pairs(symbol)
    return _get_client().get_trade_kline_history(
        trade_pairs,
        kline_type=kline_type,
        bucket_type=bucket_type,
        size=size,
    )


def get_trade_kline_miss(
    symbol: str,
    kline_type: str = "01",
    start: int | None = None,
) -> dict[str, Any]:
    """
    Get missing ranges for tradePairs kline history.
    """
    trade_pairs = _format_trade_pairs(symbol)
    return _get_client().get_trade_kline_miss(
        trade_pairs,
        kline_type=kline_type,
        start=start,
    )


def get_kline(symbol: str) -> dict[str, Any]:
    """
    获取币种K线数据

    Args:
        symbol: 币种符号

    Returns:
        K线数据
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    return _get_client().get_coin_kline(keyword)



def get_main_force(symbol: str, days: int = 90) -> dict[str, Any]:
    """??????????."""
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    client = _get_client()
    last_resp: dict[str, Any] = {"code": 500, "error": "No dense area data"}
    last_points: list[dict[str, Any]] = []

    candidate_days = [days, max(days * 2, 30), 60, 90]
    seen = set()
    for window in candidate_days:
        if window in seen:
            continue
        seen.add(window)
        resp = client.get_dense_area(keyword, window)
        last_resp = resp
        if resp.get("code") != 200:
            continue
        points = _extract_dense_points(resp)
        if not points:
            continue
        points_sorted = sorted(points, key=_point_time_ms)
        last_points = points_sorted
        has_timestamp = any(_point_time_ms(point) > 0 for point in points_sorted)
        if not has_timestamp and len(points_sorted) >= 2:
            return {"code": 200, "data": points_sorted}

        filtered = _filter_points_by_days(points_sorted, days)
        if len(filtered) >= 2:
            return {"code": 200, "data": filtered}

        widened = _filter_points_by_days(points_sorted, window)
        if len(widened) >= 2:
            return {"code": 200, "data": widened}

    if len(last_points) >= 2:
        return {"code": 200, "data": last_points[-2:]}
    if isinstance(last_resp, dict):
        return last_resp
    return {"code": 500, "error": "No dense area data"}


def get_detailed_inflow(symbol: str) -> dict[str, Any]:
    """
    获取详细资金流入数据（含多个时间周期）

    Args:
        symbol: 币种符号

    Returns:
        包含多个时间周期(5m/15m/30m/1h/4h/8h/12h/24h/2d/3d/5d/7d等)的资金流入数据:
        - stopTradeInflow: 现货资金流入
        - contractTradeInflow: 合约资金流入
        - stopTradeAmount: 现货交易量
        - contractTradeAmount: 合约交易量
        - stopTradeInflowChange: 现货流入变化率
        - contractTradeInflowChange: 合约流入变化率
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    return _get_client().get_detailed_inflow(keyword)


def get_kline_history(symbol: str, interval: str = "1h", limit: int = 500) -> dict[str, Any]:
    """
    获取K线历史数据

    Args:
        symbol: 币种符号
        interval: K线间隔 (1m/5m/15m/30m/1h/4h/1d等)
        limit: 返回数量

    Returns:
        K线历史数据
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    return _get_client().get_kline_history(keyword, interval, limit)


def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """
    搜索币种

    Args:
        query: 搜索关键词
        limit: 返回数量限制

    Returns:
        匹配的币种列表
    """
    client = _get_client()
    resp = client._request("POST", "/api/vs-token/queryCoin", json_body={
        "search": query,
        "page": 1,
        "pageSize": limit
    })

    if resp.get("code") == 200:
        return resp.get("data", {}).get("list", [])
    return []


def list_all(page: int = 1, page_size: int = 100) -> dict[str, Any]:
    """
    获取所有币种列表

    Args:
        page: 页码
        page_size: 每页数量

    Returns:
        币种列表
    """
    return _get_client().list_all_coins(page, page_size)


def get_gainers(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """获取涨幅榜"""
    return _get_client().get_coin_rank(rank_type=1, page=page, page_size=page_size)


def get_losers(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """获取跌幅榜"""
    return _get_client().get_coin_rank(rank_type=2, page=page, page_size=page_size)


def get_main_cost_rank(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """
    获取主力成本排行榜

    Returns:
        包含 cost(主力成本), deviation(偏离度), costChange(成本变化) 等字段
    """
    return _get_client().get_quality_rank(page=page, page_size=page_size)


def get_hold_cost(symbol: str, days: int = 90) -> dict[str, Any]:
    """
    获取主力成本数据（持仓成本曲线）

    API: /api/track/judge/coin/getHoldCost

    Args:
        symbol: 币种符号 (如 BTC, ETH)
        days: 查询天数，默认90天

    Returns:
        主力成本数据，包含:
        - holdingPrice: 每日主力成本价格 (如 BTC 的 $58,551.74)
        - price: 每日收盘价
        - balance: 每日余额
    """
    keyword = get_keyword(symbol)
    if not keyword:
        return {"code": 404, "msg": f"Symbol {symbol} not found"}
    return _get_client().get_hold_cost(keyword, days)


def get_token_flow(time_period: str = "H12", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """
    获取代币流向数据

    Args:
        time_period: 时间周期 (H1/H4/H8/H12/D1/D2/D3/D7/D10/D15/D30/D60/D90/D120/D150/D180, 传 all 获取全部时间窗)
        page: 页码
        page_size: 每页数量

    Returns:
        代币流入流出数据
    """
    return _get_client().get_token_flow(time_period=time_period, page=page, page_size=page_size)


def get_whale_flow(trade_type: int | None = None, time_period: str = "m5", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """
    获取主力资金流榜单

    Args:
        trade_type: 1=现货, 2=合约, None=合并现货+合约
        time_period: 时间周期 (m5/m15/m30/h1/h4/h8/h12/h24等, 传 all 获取全部时间窗)
        page: 页码
        page_size: 每页数量

    Returns:
        主力资金流数据
    """
    return _get_client().get_whale_flow(trade_type=trade_type, time_period=time_period, page=page, page_size=page_size)


def get_ai_signals(trade_type: int = 2, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """
    获取AI智能选币信号（异动看涨监控）

    Args:
        trade_type: 1=现货, 2=合约
        page: 页码
        page_size: 每页数量

    Returns:
        AI选币信号列表
    """
    return _get_client().get_ai_signals(trade_type=trade_type, page=page, page_size=page_size)


def get_opportunity_signals(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """
    获取机会看涨监控信号（AI评分系统）

    Returns:
        机会代币列表，包含AI评分、情绪、涨跌幅等
    """
    return _get_client().get_opportunity_signals(page=page, page_size=page_size)


def get_risk_signals(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """
    获取风险看跌监控信号

    Returns:
        风险代币列表，包含AI评分、风险等级、回撤等
    """
    return _get_client().get_risk_signals(page=page, page_size=page_size)


def get_valuescan_snapshot(
    symbol: str,
    days: int = 14,
    fund_flow_times: list[str] | None = None,
    volume_times: list[str] | None = None,
    heatmap_intervals: list[str] | None = None,
) -> dict[str, Any]:
    clean_symbol = symbol.upper().replace("USDT", "").replace("$", "").strip()
    keyword = get_keyword(clean_symbol)
    if not keyword:
        return {"code": 404, "error": f"Coin '{symbol}' not found"}

    snapshot: dict[str, Any] = {
        "code": 200,
        "symbol": clean_symbol,
        "keyword": keyword,
        "days": days,
    }

    basic_payload = get_basic(clean_symbol)
    if isinstance(basic_payload, dict) and basic_payload.get("code") == 200:
        snapshot["basic"] = basic_payload.get("data")

    ai_payload = get_ai_analysis(clean_symbol)
    if isinstance(ai_payload, dict) and ai_payload.get("code") == 200:
        snapshot["ai_summary"] = ai_payload.get("data")

    mf = get_main_force(clean_symbol, days)
    if isinstance(mf, dict) and mf.get("code") == 200:
        mf_data = mf.get("data") or []
        levels: list[float] = []
        for item in mf_data:
            if not isinstance(item, dict):
                continue
            price = item.get("price")
            if price is None:
                continue
            try:
                levels.append(float(price))
            except Exception:
                continue
        if levels:
            snapshot["main_force_levels"] = levels
            snapshot["current_main_force"] = levels[-1]

    hc = get_hold_cost(clean_symbol, days)
    if isinstance(hc, dict) and hc.get("code") == 200:
        hc_data = hc.get("data", {}).get("holdingPrice", [])
        if hc_data:
            try:
                snapshot["main_cost"] = float(hc_data[-1]["val"])
            except Exception:
                pass

    inflow = get_inflow(clean_symbol)
    if isinstance(inflow, dict) and inflow.get("code") == 200:
        snapshot["trade_inflow"] = inflow.get("data", {})

    detailed = get_detailed_inflow(clean_symbol)
    if isinstance(detailed, dict) and detailed.get("code") == 200:
        snapshot["detailed_inflow"] = detailed.get("data", {})

    token_flow = get_token_flow("all", 1, 20)
    if isinstance(token_flow, dict) and token_flow.get("code") == 200:
        flow_by_time = token_flow.get("data") or {}
        if flow_by_time:
            snapshot["token_flow"] = flow_by_time
            max_inflow = _extract_max_positive_inflow(flow_by_time)
            if max_inflow:
                snapshot["max_positive_inflow"] = max_inflow

    whale_flow = get_whale_flow(None, "all", 1, 20)
    if isinstance(whale_flow, dict) and whale_flow.get("code") == 200:
        whale_data = whale_flow.get("data") or {}
        if whale_data:
            snapshot["whale_flow"] = whale_data

    op_data = get_opportunity_signals(1, 10)
    if isinstance(op_data, dict) and op_data.get("code") == 200:
        snapshot["opportunity_signals"] = op_data.get("data", {})

    rs_data = get_risk_signals(1, 10)
    if isinstance(rs_data, dict) and rs_data.get("code") == 200:
        snapshot["risk_signals"] = rs_data.get("data", {})

    flow_detail_raw = get_exchange_flow_detail(clean_symbol)
    flow_detail = _normalize_exchange_flow_detail(flow_detail_raw)
    if flow_detail:
        snapshot["exchange_flow_detail"] = flow_detail
    elif isinstance(flow_detail_raw, dict) and flow_detail_raw.get("code") == 200:
        snapshot["exchange_flow_detail_raw"] = flow_detail_raw.get("data", {})

    intervals = heatmap_intervals or list(DEFAULT_HEATMAP_INTERVALS)
    heatmap_by_interval: dict[str, Any] = {}
    for interval in intervals:
        payload = get_heat_map(interval)
        if isinstance(payload, dict) and payload.get("code") == 200:
            heatmap_by_interval[interval] = payload.get("data")
    if heatmap_by_interval:
        snapshot["liquidation_heatmap_by_interval"] = heatmap_by_interval
        if "1h" in heatmap_by_interval:
            snapshot["liquidation_heatmap"] = heatmap_by_interval["1h"]

    flow_default = _load_localstorage_value("fund_history_time", "H12") or "H12"
    volume_default = _load_localstorage_value("fund_history_data_time", "H12") or "H12"

    flow_history = get_fund_trade_history_total(
        clean_symbol,
        time_particle=flow_default,
        limit_size=60,
        flow=True,
        trade_type=None,
    )
    if isinstance(flow_history, dict) and flow_history.get("code") == 200:
        snapshot["fund_flow_history"] = flow_history.get("data")

    volume_history = get_fund_trade_history_total(
        clean_symbol,
        time_particle=volume_default,
        limit_size=60,
        flow=False,
        trade_type=None,
    )
    if isinstance(volume_history, dict) and volume_history.get("code") == 200:
        snapshot["fund_volume_history"] = volume_history.get("data")

    if fund_flow_times is None:
        fund_flow_times = _load_localstorage_tags("fund-flow-history-tags", DEFAULT_FUND_FLOW_TAGS)
    if volume_times is None:
        volume_times = _load_localstorage_tags("volume-history-tags", DEFAULT_VOLUME_TAGS)

    flow_all = get_fund_trade_history_total_all(
        clean_symbol,
        times=fund_flow_times,
        limit_size=60,
        flow=True,
        trade_type=None,
    )
    if isinstance(flow_all, dict):
        if flow_all.get("data"):
            snapshot["fund_flow_history_all"] = flow_all.get("data")
        if flow_all.get("errors"):
            snapshot["fund_flow_history_all_errors"] = flow_all.get("errors")

    volume_all = get_fund_trade_history_total_all(
        clean_symbol,
        times=volume_times,
        limit_size=60,
        flow=False,
        trade_type=None,
    )
    if isinstance(volume_all, dict):
        if volume_all.get("data"):
            snapshot["fund_volume_history_all"] = volume_all.get("data")
        if volume_all.get("errors"):
            snapshot["fund_volume_history_all_errors"] = volume_all.get("errors")

    holders = get_holder_page(clean_symbol, page=1, page_size=10)
    if isinstance(holders, dict) and holders.get("code") == 200:
        snapshot["holders_top"] = holders.get("data")

    chains = get_chain_page(symbol=clean_symbol, page=1, page_size=10)
    if isinstance(chains, dict) and chains.get("code") == 200:
        snapshot["chains"] = chains.get("data")

    snapshot["meta"] = {
        "fund_flow_times": fund_flow_times,
        "volume_times": volume_times,
        "heatmap_intervals": intervals,
        "fund_flow_default": flow_default,
        "fund_volume_default": volume_default,
    }

    return snapshot


def get_all_coins() -> list[dict[str, Any]]:
    """
    获取所有币种完整列表（自动分页）

    Returns:
        所有币种列表
    """
    all_coins = []
    page = 1
    page_size = 100

    while True:
        resp = _get_client().list_all_coins(page, page_size)
        if resp.get("code") != 200:
            break

        coins = resp.get("data", {}).get("list", [])
        if not coins:
            break

        all_coins.extend(coins)
        total = resp.get("data", {}).get("total", 0)

        if len(all_coins) >= total:
            break

        page += 1

    return all_coins


def save_all_coins(filepath: str | None = None) -> str:
    """
    获取并保存所有币种信息到文件

    Args:
        filepath: 保存路径，默认为 data/all_coins.json

    Returns:
        保存的文件路径
    """
    import json
    from datetime import datetime

    coins = get_all_coins()

    if not filepath:
        data_dir = Path(__file__).parent / "data"
        data_dir.mkdir(exist_ok=True)
        filepath = str(data_dir / "all_coins.json")

    result = {
        "timestamp": datetime.now().isoformat(),
        "total": len(coins),
        "coins": coins
    }

    Path(filepath).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    return filepath


# 便捷别名
detail = get_detail
basic = get_basic
ai = get_ai_analysis
inflow = get_inflow
kline = get_kline


# 使用示例
if __name__ == "__main__":
    print("=" * 60)
    print("ValuScan 币种详情查询测试")
    print("=" * 60)

    # 测试获取 ETH 详情
    print("\n1. 获取 ETH 详情:")
    eth = get_detail("ETH")
    if eth.get("code") == 200:
        print(f"   ✓ 符号: {eth.get('symbol')}")
        print(f"   ✓ Keyword: {eth.get('keyword')}")
        print(f"   ✓ 基础信息: {'OK' if eth.get('basic') else 'None'}")
        print(f"   ✓ AI摘要: {'OK' if eth.get('ai_summary') else 'None'}")
        print(f"   ✓ 资金流入: {'OK' if eth.get('trade_inflow') else 'None'}")
    else:
        print(f"   ✗ 错误: {eth.get('error')}")

    # 测试获取 BTC 基础信息
    print("\n2. 获取 BTC 基础信息:")
    btc = get_basic("BTC")
    if btc.get("code") == 200:
        data = btc.get("data", {})
        print(f"   ✓ 名称: {data.get('name')}")
        print(f"   ✓ 价格: ${data.get('price')}")
        print(f"   ✓ 24h涨跌: {data.get('percentChange24h')}%")

    # 测试搜索
    print("\n3. 搜索 'SOL':")
    results = search("SOL", limit=5)
    for r in results[:3]:
        print(f"   - {r.get('symbol')}: {r.get('name')}")

    print("\n" + "=" * 60)
    print("测试完成")
