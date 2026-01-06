#!/usr/bin/env python3
# ruff: noqa: RUF002
"""
ValueScan Data Provider for Freqtrade
提供 ValueScan 链上数据、资金流、AI信号等给 Freqtrade 策略使用

完整集成 ValueScan 所有数据:
- 资金流分析 (现货/合约)
- 主力成本/主力位
- AI 信号系统
- 鲸鱼/大户监控
- 涨跌榜单
- K线数据
- 持仓分析
- 交易所流向
- 链上活动
- 热门/新币
"""
import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from freqtrade.valuescan_api.client import ValuScanClient
from freqtrade.valuescan_api.coin_detail import (
    get_ai_analysis,
    get_ai_signals,
    get_all_coins,
    get_basic,
    get_chain_page,
    get_detail,
    get_detailed_inflow,
    get_exchange_flow_detail,
    get_fund_trade_history_total,
    get_gainers,
    get_hold_cost,
    get_holder_page,
    get_kline,
    get_kline_history,
    get_losers,
    get_main_cost_rank,
    get_main_force,
    get_opportunity_signals,
    get_risk_signals,
    get_token_flow,
    get_whale_flow,
    list_all,
    search,
)


logger = logging.getLogger(__name__)


class ValueScanProvider:
    """
    ValueScan 数据提供器

    集成 ValueScan API 为 Freqtrade 策略提供链上数据支持

    使用示例:
        from freqtrade.valuescan_api.valuescan_provider import ValueScanProvider

        vs = ValueScanProvider()

        # 获取资金流入数据
        inflow = vs.get_fund_flow("BTC")

        # 获取 AI 信号
        signals = vs.get_ai_signals()

        # 获取主力成本
        cost = vs.get_main_force_cost("ETH")
    """

    def __init__(self, proxy: str | None = None):
        """
        初始化 ValueScan Provider

        Args:
            proxy: 代理地址，如 "socks5://127.0.0.1:7890"
        """
        self._client = ValuScanClient(proxy=proxy)
        self._cache: dict[str, Any] = {}
        self._cache_ttl: dict[str, datetime] = {}
        self._default_ttl = timedelta(minutes=5)

    def _get_cached(self, key: str) -> Any | None:
        """获取缓存数据"""
        if key in self._cache:
            if datetime.now() < self._cache_ttl.get(key, datetime.min):
                return self._cache[key]
        return None

    def _set_cached(self, key: str, value: Any, ttl: timedelta | None = None):
        """设置缓存"""
        self._cache[key] = value
        self._cache_ttl[key] = datetime.now() + (ttl or self._default_ttl)

    @staticmethod
    def _extract_records(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("records", "list", "items", "data"):
                items = data.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    # ==================== 资金流数据 ====================

    def get_fund_flow(self, symbol: str, use_cache: bool = True) -> dict[str, Any]:
        """
        获取币种资金流入数据

        Args:
            symbol: 币种符号，如 "BTC", "ETH"
            use_cache: 是否使用缓存

        Returns:
            资金流入数据，包含:
            - stopTradeInflow: 现货资金流入
            - contractTradeInflow: 合约资金流入
            - stopTradeAmount: 现货交易量
            - contractTradeAmount: 合约交易量
        """
        cache_key = f"fund_flow_{symbol}"
        if use_cache:
            cached = self._get_cached(cache_key)
            if cached:
                return cached

        result = get_detailed_inflow(symbol)
        if result.get("code") == 200:
            self._set_cached(cache_key, result)
        return result

    def get_fund_flow_df(self, symbol: str) -> pd.DataFrame:
        """
        获取资金流入数据并转换为 DataFrame

        Returns:
            包含各时间周期资金流入的 DataFrame
        """
        data = self.get_fund_flow(symbol)
        if data.get("code") != 200:
            return pd.DataFrame()

        inflow_data = data.get("data", {})
        rows = []

        for period, values in inflow_data.items():
            if isinstance(values, dict):
                rows.append({
                    "period": period,
                    "spot_inflow": values.get("stopTradeInflow", 0),
                    "contract_inflow": values.get("contractTradeInflow", 0),
                    "spot_volume": values.get("stopTradeAmount", 0),
                    "contract_volume": values.get("contractTradeAmount", 0),
                    "spot_change": values.get("stopTradeInflowChange", 0),
                    "contract_change": values.get("contractTradeInflowChange", 0),
                })

        return pd.DataFrame(rows)

    # ==================== 主力数据 ====================

    def get_main_force_cost(self, symbol: str, days: int = 90) -> dict[str, Any]:
        """
        获取主力成本数据

        Args:
            symbol: 币种符号
            days: 查询天数

        Returns:
            主力成本数据
        """
        cache_key = f"main_cost_{symbol}_{days}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        result = get_hold_cost(symbol, days)
        if result.get("code") == 200:
            self._set_cached(cache_key, result, timedelta(minutes=30))
        return result

    def get_main_force_position(self, symbol: str, days: int = 90) -> dict[str, Any]:
        """
        获取主力位数据（密集区）

        Args:
            symbol: 币种符号
            days: 查询天数

        Returns:
            主力位数据列表
        """
        return get_main_force(symbol, days)

    def get_main_cost_rank(self, page: int = 1, page_size: int = 50) -> list[dict[str, Any]]:
        """
        获取主力成本排行榜

        Returns:
            排行榜列表，包含 cost(主力成本), deviation(偏离度) 等字段
        """
        result = get_main_cost_rank(page, page_size)
        if result.get("code") == 200:
            return result.get("data", {}).get("list", [])
        return []

    # ==================== AI 信号 ====================

    def get_ai_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        获取 AI 信号列表

        Args:
            limit: 返回数量

        Returns:
            AI 信号列表
        """
        cache_key = "ai_signals"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        result = get_ai_signals(page=1, page_size=limit)
        items = self._extract_records(result)
        if items:
            self._set_cached(cache_key, items, timedelta(minutes=2))
            return items
        return []

    def get_opportunity_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取机会信号"""
        result = get_opportunity_signals(page=1, page_size=limit)
        return self._extract_records(result)

    def get_risk_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取风险信号"""
        result = get_risk_signals(page=1, page_size=limit)
        return self._extract_records(result)

    def get_coin_ai_analysis(self, symbol: str) -> dict[str, Any]:
        """
        获取特定币种的 AI 分析

        Returns:
            AI 分析结果，包含看涨/看跌/中立情绪
        """
        return get_ai_analysis(symbol)

    # ==================== 涨跌榜 ====================

    def get_top_gainers(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        获取涨幅榜

        Returns:
            涨幅排名前N的币种列表
        """
        result = get_gainers(page=1, page_size=limit)
        if result.get("code") == 200:
            return result.get("data", {}).get("list", [])
        return []

    def get_top_losers(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        获取跌幅榜

        Returns:
            跌幅排名前N的币种列表
        """
        result = get_losers(page=1, page_size=limit)
        if result.get("code") == 200:
            return result.get("data", {}).get("list", [])
        return []

    # ==================== 代币详情 ====================

    def get_coin_detail(self, symbol: str) -> dict[str, Any]:
        """
        获取币种完整详情

        Returns:
            包含基础信息、AI分析、资金流入、交易所信息等
        """
        return get_detail(symbol)

    def get_coin_basic(self, symbol: str) -> dict[str, Any]:
        """
        获取币种基础信息

        Returns:
            价格、市值、涨跌幅等
        """
        return get_basic(symbol)

    # ==================== 鲸鱼/大户数据 ====================

    def get_whale_flow(self, symbol: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """
        获取鲸鱼资金流向

        Returns:
            大户资金流动数据
        """
        time_period = symbol or "m5"
        result = get_whale_flow(trade_type=1, time_period=time_period, page=1, page_size=limit)
        return self._extract_records(result)

    def get_token_flow(self, time_period: str = "H12", limit: int = 20) -> list[dict[str, Any]]:
        """
        获取代币流向数据

        Args:
            time_period: 时间周期 (H1/H4/H8/H12/D1/D7等)
            limit: 返回数量

        Returns:
            代币流入流出数据
        """
        result = get_token_flow(time_period=time_period, page=1, page_size=limit)
        return self._extract_records(result)

    # ==================== 策略辅助方法 ====================

    def is_bullish_signal(self, symbol: str) -> bool:
        """
        判断币种是否有看涨信号

        基于资金流入、AI分析综合判断
        """
        try:
            # 检查资金流入
            inflow = self.get_fund_flow(symbol)
            if inflow.get("code") != 200:
                return False

            data = inflow.get("data", {})
            # 检查 1h 和 4h 资金流入
            h1 = data.get("1h", {})
            h4 = data.get("4h", {})

            h1_inflow = h1.get("stopTradeInflow", 0) + h1.get("contractTradeInflow", 0)
            h4_inflow = h4.get("stopTradeInflow", 0) + h4.get("contractTradeInflow", 0)

            # 检查 AI 分析
            ai = self.get_coin_ai_analysis(symbol)
            ai_data = ai.get("data", {})
            bullish_ratio = ai_data.get("bullishRatio", 0) if ai_data else 0

            # 综合判断: 短期资金流入为正且 AI 看涨比例 > 50%
            return h1_inflow > 0 and h4_inflow > 0 and bullish_ratio > 50

        except Exception as e:
            logger.warning(f"Error checking bullish signal for {symbol}: {e}")
            return False

    def is_bearish_signal(self, symbol: str) -> bool:
        """
        判断币种是否有看跌信号

        基于资金流出、AI分析综合判断
        """
        try:
            inflow = self.get_fund_flow(symbol)
            if inflow.get("code") != 200:
                return False

            data = inflow.get("data", {})
            h1 = data.get("1h", {})
            h4 = data.get("4h", {})

            h1_inflow = h1.get("stopTradeInflow", 0) + h1.get("contractTradeInflow", 0)
            h4_inflow = h4.get("stopTradeInflow", 0) + h4.get("contractTradeInflow", 0)

            ai = self.get_coin_ai_analysis(symbol)
            ai_data = ai.get("data", {})
            bearish_ratio = ai_data.get("bearishRatio", 0) if ai_data else 0

            return h1_inflow < 0 and h4_inflow < 0 and bearish_ratio > 50

        except Exception as e:
            logger.warning(f"Error checking bearish signal for {symbol}: {e}")
            return False

    def get_signal_strength(self, symbol: str) -> float:
        """
        获取信号强度 (-1.0 到 1.0)

        正值表示看涨，负值表示看跌，绝对值表示强度
        """
        try:
            inflow = self.get_fund_flow(symbol)
            if inflow.get("code") != 200:
                return 0.0

            data = inflow.get("data", {})

            # 计算各周期权重
            weights = {"5m": 0.1, "15m": 0.15, "30m": 0.15, "1h": 0.2, "4h": 0.2, "24h": 0.2}
            total_score = 0.0
            total_weight = 0.0

            for period, weight in weights.items():
                period_data = data.get(period, {})
                if period_data:
                    spot = period_data.get("stopTradeInflow", 0)
                    contract = period_data.get("contractTradeInflow", 0)
                    total = spot + contract

                    # 归一化分数 (-1 到 1)
                    if total != 0:
                        score = 1.0 if total > 0 else -1.0
                        total_score += score * weight
                        total_weight += weight

            if total_weight > 0:
                return total_score / total_weight
            return 0.0

        except Exception as e:
            logger.warning(f"Error calculating signal strength for {symbol}: {e}")
            return 0.0

    def clear_cache(self):
        """清除所有缓存"""
        self._cache.clear()
        self._cache_ttl.clear()

    # ==================== K线数据 ====================

    def get_kline(self, symbol: str) -> dict[str, Any]:
        """
        获取币种K线数据

        Args:
            symbol: 币种符号

        Returns:
            K线数据
        """
        return get_kline(symbol)

    def get_kline_history(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500,
    ) -> dict[str, Any]:
        """
        获取K线历史数据

        Args:
            symbol: 币种符号
            interval: K线间隔 (1m/5m/15m/30m/1h/4h/1d等)
            limit: 返回数量

        Returns:
            K线历史数据
        """
        cache_key = f"kline_{symbol}_{interval}_{limit}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        result = get_kline_history(symbol, interval, limit)
        if result.get("code") == 200:
            self._set_cached(cache_key, result, timedelta(minutes=1))
        return result

    def get_kline_df(self, symbol: str, interval: str = "1h", limit: int = 500) -> pd.DataFrame:
        """
        获取K线数据并转为 DataFrame

        Returns:
            包含 open, high, low, close, volume 的 DataFrame
        """
        data = self.get_kline_history(symbol, interval, limit)
        if data.get("code") != 200:
            return pd.DataFrame()

        klines = data.get("data", [])
        if not klines:
            return pd.DataFrame()

        df = pd.DataFrame(klines)
        # 标准化列名
        column_map = {
            "openTime": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume"
        }
        df = df.rename(columns=column_map)

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], unit="ms")
            df = df.set_index("date")

        return df

    # ==================== 交易所流向 ====================

    def get_exchange_flow(self, symbol: str) -> dict[str, Any]:
        """
        获取交易所资金流向详情

        Args:
            symbol: 币种符号

        Returns:
            包含各时间周期的流入/流出数据
        """
        return get_exchange_flow_detail(symbol)

    def get_exchange_flow_df(self, symbol: str) -> pd.DataFrame:
        """
        获取交易所流向并转为 DataFrame
        """
        data = self.get_exchange_flow(symbol)
        if data.get("code") != 200:
            return pd.DataFrame()

        flow_data = data.get("data", {})
        rows = []

        for period, values in flow_data.items():
            if isinstance(values, dict):
                rows.append({
                    "period": period,
                    "inflow": values.get("inAmount", 0),
                    "inflow_value": values.get("inValue", 0),
                    "outflow": values.get("outAmount", 0),
                    "outflow_value": values.get("outValue", 0),
                    "net_flow": values.get("inFlowValue", 0),
                    "net_change": values.get("inFlowValueChange", 0),
                })

        return pd.DataFrame(rows)

    # ==================== 持仓分析 ====================

    def get_holders(self, symbol: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """
        获取持仓排名

        Args:
            symbol: 币种符号
            page: 页码
            page_size: 每页数量

        Returns:
            持仓排名列表
        """
        return get_holder_page(symbol, page, page_size)

    def get_holders_df(self, symbol: str, limit: int = 50) -> pd.DataFrame:
        """获取持仓排名并转为 DataFrame"""
        data = self.get_holders(symbol, page_size=limit)
        if data.get("code") != 200:
            return pd.DataFrame()

        holders = data.get("data", {}).get("list", [])
        return pd.DataFrame(holders) if holders else pd.DataFrame()

    # ==================== 资金历史 ====================

    def get_fund_history(
        self,
        symbol: str,
        time_particle: str = "12h",
        limit: int = 100,
        flow: bool = True,
        trade_type: int = 2
    ) -> dict[str, Any]:
        """
        获取资金流/成交量历史数据

        Args:
            symbol: 币种符号
            time_particle: 时间粒度 (1h/4h/12h/24h等)
            limit: 返回数量
            flow: True=资金流, False=成交量
            trade_type: 1=现货, 2=合约

        Returns:
            历史数据列表
        """
        return get_fund_trade_history_total(
            symbol, time_particle, limit, flow, trade_type
        )

    def get_fund_history_df(
        self,
        symbol: str,
        time_particle: str = "12h",
        limit: int = 100
    ) -> pd.DataFrame:
        """获取资金流历史并转为 DataFrame"""
        data = self.get_fund_history(symbol, time_particle, limit, flow=True)
        if data.get("code") != 200:
            return pd.DataFrame()

        history = data.get("data", [])
        if not history:
            return pd.DataFrame()

        df = pd.DataFrame(history)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="ms")
            df = df.set_index("time")

        return df

    # ==================== 链信息 ====================

    def get_chains(
        self,
        symbol: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        """
        获取代币所在链信息

        Args:
            symbol: 币种符号 (可选)

        Returns:
            链列表
        """
        result = get_chain_page(symbol, page, page_size)
        if result.get("code") == 200:
            return result.get("data", {}).get("list", [])
        return []

    # ==================== 搜索和列表 ====================

    def search_coin(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """
        搜索币种

        Args:
            query: 搜索关键词
            limit: 返回数量

        Returns:
            匹配的币种列表
        """
        return search(query, limit)

    def get_all_coins(self) -> list[dict[str, Any]]:
        """
        获取所有币种列表

        Returns:
            完整币种列表
        """
        cache_key = "all_coins"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        result = get_all_coins()
        if result:
            self._set_cached(cache_key, result, timedelta(hours=1))
        return result

    def list_coins(self, page: int = 1, page_size: int = 100) -> dict[str, Any]:
        """分页获取币种列表"""
        return list_all(page, page_size)

    # ==================== 资金异动 ====================

    def get_funds_movement(self, page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """
        获取资金异动列表

        Returns:
            最新资金异动事件
        """
        cache_key = f"funds_movement_{page}"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        result = self._client.get_funds_movement(page, page_size)
        if result.get("code") == 200:
            self._set_cached(cache_key, result, timedelta(minutes=2))
        return result

    def get_funds_movement_list(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取资金异动列表"""
        result = self.get_funds_movement(page_size=limit)
        if result.get("code") == 200:
            data = result.get("data", {})
            return data.get("records", []) or data.get("list", [])
        return []

    # ==================== 热门币种 ====================

    def get_trending(self, page: int = 1, page_size: int = 20) -> list[dict[str, Any]]:
        """
        获取热门代币

        Returns:
            热门币种列表
        """
        result = self._client.get_trending(page, page_size)
        if result.get("code") == 200:
            return result.get("data", {}).get("list", [])
        return []

    def get_new_listings(self, page: int = 1, page_size: int = 20) -> list[dict[str, Any]]:
        """
        获取新上线代币

        Returns:
            新币列表
        """
        result = self._client.get_new_listings(page, page_size)
        if result.get("code") == 200:
            return result.get("data", {}).get("list", [])
        return []

    def get_hot_coins(self, trade_type: int = 1) -> dict[str, Any]:
        """
        获取交易热门币种

        Args:
            trade_type: 1=现货, 2=合约

        Returns:
            包含24小时和90天热门
        """
        return self._client.get_trade_coin_top(trade_type)

    # ==================== 市场概览 ====================

    def get_market_overview(self) -> dict[str, Any]:
        """
        获取市场概览 (聚合多个数据源)

        Returns:
            包含信号、涨跌榜、资金异动等综合数据
        """
        cache_key = "market_overview"
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        result = self._client.get_market_overview()
        if result.get("code") == 200:
            self._set_cached(cache_key, result, timedelta(minutes=2))
        return result

    # ==================== 综合分析方法 ====================

    def analyze_coin(self, symbol: str) -> dict[str, Any]:
        """
        综合分析一个币种

        Returns:
            包含所有维度数据的综合分析结果
        """
        analysis: dict[str, Any] = {
            "symbol": symbol.upper(),
            "timestamp": datetime.now().isoformat(),
            "basic": None,
            "fund_flow": None,
            "exchange_flow": None,
            "ai_analysis": None,
            "main_force": None,
            "signal_strength": 0.0,
            "is_bullish": False,
            "is_bearish": False,
            "recommendation": "NEUTRAL",
        }

        try:
            # 基础信息
            basic = self.get_coin_basic(symbol)
            if basic.get("code") == 200:
                analysis["basic"] = basic.get("data")

            # 资金流
            fund_flow = self.get_fund_flow(symbol)
            if fund_flow.get("code") == 200:
                analysis["fund_flow"] = fund_flow.get("data")

            # 交易所流向
            exchange_flow = self.get_exchange_flow(symbol)
            if exchange_flow.get("code") == 200:
                analysis["exchange_flow"] = exchange_flow.get("data")

            # AI 分析
            ai = self.get_coin_ai_analysis(symbol)
            if ai.get("code") == 200:
                analysis["ai_analysis"] = ai.get("data")

            # 主力成本
            main_force = self.get_main_force_cost(symbol)
            if main_force.get("code") == 200:
                analysis["main_force"] = main_force.get("data")

            # 信号强度
            analysis["signal_strength"] = self.get_signal_strength(symbol)
            analysis["is_bullish"] = self.is_bullish_signal(symbol)
            analysis["is_bearish"] = self.is_bearish_signal(symbol)

            # 综合推荐
            if analysis["is_bullish"] and analysis["signal_strength"] > 0.5:
                analysis["recommendation"] = "STRONG_BUY"
            elif analysis["is_bullish"]:
                analysis["recommendation"] = "BUY"
            elif analysis["is_bearish"] and analysis["signal_strength"] < -0.5:
                analysis["recommendation"] = "STRONG_SELL"
            elif analysis["is_bearish"]:
                analysis["recommendation"] = "SELL"
            else:
                analysis["recommendation"] = "NEUTRAL"

        except Exception as e:
            logger.error(f"Error analyzing {symbol}: {e}")
            analysis["error"] = str(e)

        return analysis

    def get_trading_candidates(
        self,
        min_signal_strength: float = 0.3,
        limit: int = 20
    ) -> list[dict[str, Any]]:
        """
        获取交易候选币种

        基于 AI 信号和资金流筛选

        Args:
            min_signal_strength: 最小信号强度
            limit: 返回数量

        Returns:
            候选币种列表，按信号强度排序
        """
        candidates = []

        # 从机会信号获取候选
        opportunities = self.get_opportunity_signals(limit=50)
        for opp in opportunities:
            symbol = opp.get("symbol", "").upper()
            if not symbol:
                continue

            try:
                strength = self.get_signal_strength(symbol)
                if strength >= min_signal_strength:
                    candidates.append({
                        "symbol": symbol,
                        "signal_strength": strength,
                        "ai_score": opp.get("score", 0),
                        "bullish_ratio": opp.get("bullishRatio", 0),
                        "gains_24h": opp.get("gains", 0),
                        "source": "opportunity",
                    })
            except Exception as e:
                logger.debug(f"Skip {symbol}: {e}")

        # 从涨幅榜获取候选
        gainers = self.get_top_gainers(limit=30)
        for gainer in gainers:
            symbol = gainer.get("symbol", "").upper()
            if not symbol or any(c["symbol"] == symbol for c in candidates):
                continue

            try:
                strength = self.get_signal_strength(symbol)
                if strength >= min_signal_strength:
                    candidates.append({
                        "symbol": symbol,
                        "signal_strength": strength,
                        "gains_24h": gainer.get("percentChange24h", 0),
                        "market_cap": gainer.get("marketCap", 0),
                        "source": "gainer",
                    })
            except Exception as e:
                logger.debug(f"Skip {symbol}: {e}")

        # Sort by signal strength.
        candidates.sort(key=lambda x: x["signal_strength"], reverse=True)

        return candidates[:limit]

    def get_risk_coins(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        获取风险币种列表

        Returns:
            需要规避的高风险币种
        """
        risk_list: list[dict[str, Any]] = []

        # 从风险信号获取
        risks = self.get_risk_signals(limit=50)
        for risk in risks:
            symbol = risk.get("symbol", "").upper()
            if not symbol:
                continue

            try:
                strength = self.get_signal_strength(symbol)
                risk_list.append({
                    "symbol": symbol,
                    "signal_strength": strength,
                    "risk_score": risk.get("score", 0),
                    "bearish_ratio": risk.get("bearishRatio", 0),
                    "retracement": risk.get("retracement", 0),
                })
            except Exception as exc:
                logger.debug("Skip risk coin %s: %s", symbol, exc)

        # Sort by signal strength (most negative first).
        risk_list.sort(key=lambda x: x["signal_strength"])

        return risk_list[:limit]

    # ==================== DataFrame 输出 ====================

    def to_indicator_df(self, symbol: str) -> pd.DataFrame:
        """
        将所有数据整合为可用于策略的 DataFrame

        Returns:
            包含所有指标的 DataFrame
        """
        # 获取K线数据作为基础
        kline_df = self.get_kline_df(symbol, interval="1h", limit=200)
        if kline_df.empty:
            return pd.DataFrame()

        # 添加资金流指标
        fund_flow = self.get_fund_flow(symbol)
        if fund_flow.get("code") == 200:
            data = fund_flow.get("data", {})
            for period in ["1h", "4h", "24h"]:
                period_data = data.get(period, {})
                kline_df[f"spot_inflow_{period}"] = period_data.get("stopTradeInflow", 0)
                kline_df[f"contract_inflow_{period}"] = period_data.get("contractTradeInflow", 0)
                kline_df[f"total_inflow_{period}"] = (
                    period_data.get("stopTradeInflow", 0) +
                    period_data.get("contractTradeInflow", 0)
                )

        # 添加信号强度
        kline_df["vs_signal_strength"] = self.get_signal_strength(symbol)
        kline_df["vs_is_bullish"] = self.is_bullish_signal(symbol)
        kline_df["vs_is_bearish"] = self.is_bearish_signal(symbol)

        # 添加 AI 分析
        ai = self.get_coin_ai_analysis(symbol)
        if ai.get("code") == 200:
            ai_data = ai.get("data", {})
            kline_df["vs_bullish_ratio"] = ai_data.get("bullishRatio", 0)
            kline_df["vs_bearish_ratio"] = ai_data.get("bearishRatio", 0)
            kline_df["vs_neutral_ratio"] = ai_data.get("neutralRatio", 0)

        return kline_df


# 全局实例
_provider: ValueScanProvider | None = None


def get_provider(proxy: str | None = None) -> ValueScanProvider:
    """获取全局 ValueScan Provider 实例"""
    global _provider
    if _provider is None:
        _provider = ValueScanProvider(proxy=proxy)
    return _provider


def reset_provider():
    """重置全局 Provider 实例"""
    global _provider
    if _provider:
        _provider.clear_cache()
    _provider = None
