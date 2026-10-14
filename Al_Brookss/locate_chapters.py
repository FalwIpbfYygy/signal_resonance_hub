# -*- coding: utf-8 -*-
"""定位三本书各章节的行号边界，生成章节索引"""
import re, os

BASE = r"Al Brooks价格行为"
FILES = ["趋势篇.txt", "区间篇.txt", "反转篇.txt"]

# 章节标题模式：第X章
CHAP_PAT = re.compile(r"第\s*(\d+)\s*章\s*(.+)")

for fname in FILES:
    fpath = os.path.join(BASE, fname)
    print("=" * 80)
    print(f"【{fname}】")
    print("=" * 80)
    with open(fpath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    chapters = []
    for i, line in enumerate(lines):
        m = CHAP_PAT.search(line.strip())
        if m:
            # 过滤掉目录里的章节（目录页通常在前面，且含点号引导）
            if "....." not in line and len(line.strip()) < 60:
                chapters.append((i + 1, m.group(1), m.group(2).strip()))
    for lineno, num, title in chapters:
        print(f"  行 {lineno:>5}  第{num}章  {title}")
    print(f"  总行数: {len(lines)}")
    print()
