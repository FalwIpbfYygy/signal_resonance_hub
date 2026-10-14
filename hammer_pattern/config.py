# -*- coding: utf-8 -*-
import os
"""锤子/针形形态检测器 —— 默认参数（均可被 CLI 覆盖，不写死）。"""

DEFAULTS = {
    # 数据
    "symbol": "",
    "interval": "5m",
    "limit": 1000,                 # Binance klines 上限 1500
    # 锤子判定阈值（已用真实数据验证，见 PRD §5）
    "lower_ratio_min": 0.55,       # 下影占全幅下限
    "upper_ratio_max": 0.15,       # 上影占全幅上限（关键：剔除十字星）
    "body_ratio_max": 0.30,        # 实体占全幅上限
    "close_pos_min": 0.60,         # 收盘位置下限（收在上部）
    "lower_to_body_min": 2.0,      # 下影/实体 下限
    # 放量过滤（默认开启）
    "volume_filter": True,
    "vol_ma_window": 20,           # 成交量 MA 窗口
    "vol_mult": 1.8,              # 放量倍数阈值
    # 网络
    "proxy": os.environ.get("HAMMER_PROXY", ""),  # 本地开发机代理；服务器部署时置空（零代理纪律）
}


def merged(overrides=None):
    """返回合并后的参数字典（overrides 为 CLI 传入的部分覆盖）。"""
    d = dict(DEFAULTS)
    if overrides:
        d.update({k: v for k, v in overrides.items() if v is not None})
    return d
