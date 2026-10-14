# -*- coding: utf-8 -*-
"""提取三本 Al Brooks PDF 的完整目录结构"""
import pymupdf
import os
import re

PDF_DIR = r"Al Brooks价格行为"
PDFS = [
    ("趋势篇", "1.AL Brooks《价格行为交易之趋势篇》.pdf"),
    ("区间篇", "2.AL Brooks《价格行为交易之区间篇》.pdf"),
    ("反转篇", "3.AL Brooks《价格行为交易之反转篇》.pdf"),
]

for label, pdf_name in PDFS:
    pdf_path = os.path.join(PDF_DIR, pdf_name)
    print("=" * 90)
    print(f"【{label}】 {pdf_name}")
    print("=" * 90)
    doc = pymupdf.open(pdf_path)
    # 目录页通常在第2页（索引1）
    toc_text = doc[1].get_text("text")
    print(toc_text)
    doc.close()
    print()
