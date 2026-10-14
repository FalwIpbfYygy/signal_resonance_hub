# -*- coding: utf-8 -*-
"""提取 Al Brooks 三本价格行为 PDF 的文本内容"""
import fitz  # PyMuPDF
import os

PDF_DIR = r"Al Brooks价格行为"
PDFS = [
    "1.AL Brooks《价格行为交易之趋势篇》.pdf",
    "2.AL Brooks《价格行为交易之区间篇》.pdf",
    "3.AL Brooks《价格行为交易之反转篇》.pdf",
]

for pdf_name in PDFS:
    pdf_path = os.path.join(PDF_DIR, pdf_name)
    print("=" * 80)
    print(f"FILE: {pdf_name}")
    print("=" * 80)
    try:
        doc = fitz.open(pdf_path)
        print(f"总页数: {len(doc)}")
        # 仅先输出前 3 页探测文本可提取性
        for i in range(min(3, len(doc))):
            page = doc[i]
            text = page.get_text("text")
            print(f"\n----- Page {i+1} (chars={len(text)}) -----")
            print(text[:2000])
        doc.close()
    except Exception as e:
        print(f"ERROR: {e}")
    print()
