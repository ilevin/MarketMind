"""量化数据查询服务子包（etf-data-module，design D10）。

V1 仅 ETF：`etf_data.EtfDataService` 提供统一查询
``get_etf_daily(symbol, start, end, adjust)``，动态计算复权价
（复权价永不入库）。股票复权查询另行演进（design Non-Goals）。
"""
