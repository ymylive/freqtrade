#!/usr/bin/env python3
"""
ValuScan API 模块
提供 ValuScan 数据获取和币种详情查询功能

使用示例:
    from valuescan_api import get_detail, get_basic, search

    # 通过币种名称获取完整详情
    eth = get_detail("ETH")

    # 获取基础信息
    btc = get_basic("BTC")

    # 搜索币种
    results = search("SOL")
"""

from .client import ValuScanClient, get_client
from .coin_detail import (
    ai,
    basic,
    detail,
    get_ai_analysis,
    get_ai_signals,
    get_all_coins,
    get_basic,
    get_chain_page,
    get_detail,
    get_detailed_inflow,
    get_exchange_flow_detail,
    get_fund_trade_history_total,
    get_fund_trade_history_total_all,
    get_gainers,
    get_heat_map,
    get_hold_cost,
    get_holder_page,
    get_inflow,
    get_keyword,
    get_kline,
    get_kline_history,
    get_kline_time,
    get_losers,
    get_main_cost_rank,
    get_main_force,
    get_opportunity_signals,
    get_risk_signals,
    get_token_flow,
    get_trade_kline_history,
    get_trade_kline_miss,
    get_valuescan_snapshot,
    get_whale_flow,
    inflow,
    kline,
    list_all,
    save_all_coins,
    search,
)
from .valuescan_provider import ValueScanProvider, get_provider


__all__ = [
    "ValuScanClient",
    "get_client",
    "ValueScanProvider",
    "get_provider",
    "get_detail",
    "get_basic",
    "get_ai_analysis",
    "get_inflow",
    "get_exchange_flow_detail",
    "get_heat_map",
    "get_fund_trade_history_total",
    "get_fund_trade_history_total_all",
    "get_holder_page",
    "get_chain_page",
    "get_kline_time",
    "get_trade_kline_history",
    "get_trade_kline_miss",
    "get_kline",
    "get_keyword",
    "search",
    "list_all",
    "get_all_coins",
    "save_all_coins",
    "get_main_force",
    "get_detailed_inflow",
    "get_kline_history",
    "get_gainers",
    "get_losers",
    "get_main_cost_rank",
    "get_hold_cost",
    "get_token_flow",
    "get_whale_flow",
    "get_ai_signals",
    "get_opportunity_signals",
    "get_risk_signals",
    "get_valuescan_snapshot",
    "detail",
    "basic",
    "ai",
    "inflow",
    "kline",
]
