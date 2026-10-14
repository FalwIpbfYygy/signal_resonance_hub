# -*- coding: utf-8 -*-
"""提取趋势篇术语列表（第6-35页）"""
import pymupdf

doc = pymupdf.open(r"Al Brooks价格行为\1.AL Brooks《价格行为交易之趋势篇》.pdf")
# 目录显示术语列表在 -6 - 到 -35 - 页（PDF物理页索引5到34）
for i in range(5, 36):
    text = doc[i].get_text("text")
    print(f"\n========== PDF Page {i+1} ==========")
    print(text)
doc.close()
