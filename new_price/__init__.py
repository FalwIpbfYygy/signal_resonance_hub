# ICT  交易策略系统

'''
ICT/ict_/
├── 道氏_main.py                         # 主程序入口（10秒轮询+飞书推送）
├── config.py                       # 配置中心（品种/时间框架/飞书/ATR/去重）
├── test_indicators.py              # 指标功能验证脚本
├── data/
│   ├── candle.py                   # 数据结构（Candle/FVG/Swing/CSD/OB/KeyLevel）
│   └── binance_api.py             # Binance K线拉取+标准化
├── indicators/
│   ├── swing.py                    # Swing High/Low识别（3-bar标准）
│   ├── fvg.py                      # FVG识别+反转检测+BSG判断+拐点
│   ├── csd.py                      # CSD交付状态改变（实体吞噬）
│   └── atr.py                      # ATR + SFP + 流动性清扫检测
├── levels/
│   ├── time_levels.py              # 时间级别标记（亚洲/伦敦/前日/周高低/FVG级别）
│   └── level_manager.py            # 级别状态管理（UNTOUCHED/TOUCHED/CONSUMED）
├── signals/
│   └── engine.py                   # 信号检测引擎（核心：级别触及+确认组合+去重）
├── notify/
│   └── feishu.py                   # 飞书推送（格式化模板+去重）
└── utils/
    └── time_utils.py               # 时区转换+黄金时段识别（7个时段+权重）


'''