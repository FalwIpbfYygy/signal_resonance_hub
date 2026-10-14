"""级别状态管理"""
from typing import List, Dict
from data.candle import KeyLevel


class LevelManager:
    """管理关键级别的状态（UNTOUCHED / TOUCHED）。

    仅维护「未触及 → 已触及」两态：每次扫描都重建 LevelManager，
    级别消耗/翻转无法跨扫描持久化，去重实际由 ICT_main 的级别冷却（30 分钟）承担。
    """

    def __init__(self, levels: List[KeyLevel]):
        self.levels: Dict[str, KeyLevel] = {}
        for lv in levels:
            key = f"{lv.name}_{lv.price:.2f}"
            self.levels[key] = lv

    def update(self, current_price: float, buffer_pct: float = 0.05):
        """根据当前价格更新所有级别状态：价格进入缓冲区间则标记为已触及。"""
        buffer = current_price * buffer_pct / 100.0

        for lv in self.levels.values():
            dist = abs(current_price - lv.price)
            if dist <= buffer and lv.status == "UNTOUCHED":
                lv.status = "TOUCHED"

    def get_touched(self) -> List[KeyLevel]:
        """获取所有已触及的级别"""
        return [lv for lv in self.levels.values() if lv.status == "TOUCHED"]

    def get_untouched(self) -> List[KeyLevel]:
        """获取所有未触及的级别"""
        return [lv for lv in self.levels.values() if lv.status == "UNTOUCHED"]
