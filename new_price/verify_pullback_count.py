# -*- coding: utf-8 -*-
"""
验证脚本: 数推/回调计数(高123/低123)
  1) 合成棒线自测 —— 锁定两条计数陷阱(确定性断言)
  2) 拉取 5m K线 —— 逐棒打印高/低计数等级
用法: python verify_pullback_count.py
"""
import config
import data
import signal_bars as sb
import pullback_count as pc


def make_bar(high, low, open_=None, close=None):
    """构造合成棒线。open/close 决定实体方向，默认按 high/low 中点外推。"""
    if open_ is None:
        open_ = (high + low) / 2
    if close is None:
        close = high if high > low else low
    return sb.BarData(index=0, open=open_, high=high, low=low, close=close)


def self_test():
    """两组确定性自测，直接对拍锁定定义的两条陷阱。"""
    print("=" * 50)
    print("自测: 计数陷阱(锁定定义 学委方方土PA.txt L282/283)")
    print("=" * 50)

    # 陷阱1: 回调后连续两根高点抬高 → 仍只算高1, 不算高2
    # 构造: 上涨 → 回调(低点下降) → 连续破高两根
    bars = [
        make_bar(10.0, 9.8),    # 0 基准
        make_bar(10.3, 10.0),   # 1 上涨
        make_bar(10.1, 9.7),    # 2 回调(低点<前低)
        make_bar(10.2, 9.9),    # 3 破高第一根 → 高1
        make_bar(10.4, 10.2),   # 4 连续破高(未回调) → 不应算高2
    ]
    for i, b in enumerate(bars):
        b.index = i
    hl = pc.count_high_levels(bars)
    assert hl[3] == 1, f"陷阱1失败: 期望 bar3=高1, 实得 {hl[3]}"
    assert hl[4] == 0, f"陷阱1失败: 期望 bar4=0(连续破高不算高2), 实得 {hl[4]}"
    print(f"  陷阱1 通过: bar3=高{hl[3]}, bar4={hl[4]}(连续破高不计数)")

    # 陷阱2: 二次回调(中间无新高) → 高2; 之后创新高 → 重算高1
    bars2 = [
        make_bar(10.0, 9.8),    # 0
        make_bar(10.3, 10.0),   # 1 上涨 → 参考峰=10.3
        make_bar(10.1, 9.7),    # 2 回调
        make_bar(10.2, 9.9),    # 3 高1(未创新高)
        make_bar(10.1, 9.6),    # 4 再回调(低点<前低)
        make_bar(10.25, 9.95),  # 5 高2(未创新高)
        make_bar(10.6, 10.3),   # 6 创新高(>10.3) → 重置
        make_bar(10.4, 10.2),   # 7 回调
        make_bar(10.6, 10.4),   # 8 破高 → 应重新算高1
    ]
    for i, b in enumerate(bars2):
        b.index = i
    hl2 = pc.count_high_levels(bars2)
    assert hl2[3] == 1, f"陷阱2失败: bar3 期望高1, 实得 {hl2[3]}"
    assert hl2[5] == 2, f"陷阱2失败: bar5 期望高2, 实得 {hl2[5]}"
    assert hl2[8] == 1, f"陷阱2失败: bar8(创新高后) 期望重新高1, 实得 {hl2[8]}"
    print(f"  陷阱2 通过: bar3=高{hl2[3]}, bar5=高{hl2[5]}, bar8重算=高{hl2[8]}")

    # 空头对称自测(低123)
    bars3 = [
        make_bar(10.5, 10.2),   # 0 基准
        make_bar(10.2, 9.9),    # 1 下跌
        make_bar(10.4, 10.1),   # 2 回调(高点>前高)
        make_bar(10.3, 9.8),    # 3 低点9.8<10.1 破低 → 低1
        make_bar(10.2, 9.7),    # 4 继续下跌
    ]
    for i, b in enumerate(bars3):
        b.index = i
    ll = pc.count_low_levels(bars3)
    assert ll[3] == 1, f"空头自测失败: bar3 期望低1, 实得 {ll[3]}"
    print(f"  空头对称 通过: bar3=低{ll[3]}")

    print("自测全部通过\n")


def live_scan():
    interval = config.PRIMARY_INTERVAL
    cache = data.KlineCache()
    raw = data.fetch_klines(interval)
    if not raw:
        print("拉取失败")
        raise SystemExit(1)
    cache.update(interval, raw)
    arr = cache.get_arrays(interval)
    bars = sb.build_bars(arr)

    high_levels, low_levels = pc.count_pullback_levels(bars)

    # 近 20 根逐棒打印
    print(f"近20根{interval}棒线数推计数:")
    start = max(0, len(bars) - 20)
    for i in range(start, len(bars)):
        b = bars[i]
        h, l = high_levels[i], low_levels[i]
        tag = ""
        if h:
            tag = f" 高{h}"
        if l:
            tag += f" 低{l}"
        print(f"  #{i:4d} H={b.high:7.2f} L={b.low:7.2f} C={b.close:7.2f}{tag}")

    # 统计分布
    def dist(levels):
        c = {1: 0, 2: 0, 3: 0, 4: 0}
        for v in levels:
            if v in c:
                c[v] += 1
        return c

    hd, ld = dist(high_levels), dist(low_levels)
    print(f"\n高计数分布(近{len(bars)}棒): 高1={hd[1]} 高2={hd[2]} 高3={hd[3]} 高4={hd[4]}")
    print(f"低计数分布(近{len(bars)}棒): 低1={ld[1]} 低2={ld[2]} 低3={ld[3]} 低4={ld[4]}")
    print("验证完成")


if __name__ == "__main__":
    self_test()
    live_scan()