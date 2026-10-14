# -*- coding: utf-8 -*-
"""将三本 Al Brooks PDF 全文提取为 txt 文件，建立原文语料库"""
import pymupdf
import os

PDF_DIR = r"Al Brooks价格行为"
PDFS = [
    ("趋势篇.txt", "1.AL Brooks《价格行为交易之趋势篇》.pdf"),
    ("区间篇.txt", "2.AL Brooks《价格行为交易之区间篇》.pdf"),
    ("反转篇.txt", "3.AL Brooks《价格行为交易之反转篇》.pdf"),
]

for out_name, pdf_name in PDFS:
    pdf_path = os.path.join(PDF_DIR, pdf_name)
    out_path = os.path.join(PDF_DIR, out_name)
    print(f"提取: {pdf_name} -> {out_name}")
    doc = pymupdf.open(pdf_path)
    all_text = []
    for i in range(len(doc)):
        text = doc[i].get_text("text")
        all_text.append(f"\n========== [Page {i+1}] ==========\n{text}")
    doc.close()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(all_text))
    print(f"  完成: {len(all_text)} 页, {os.path.getsize(out_path)} 字节")
print("全部提取完成")
