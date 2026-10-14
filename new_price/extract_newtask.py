# -*- coding: utf-8 -*-
"""提取 new_task 两本 PA 笔记 PDF 的文本内容"""
import fitz
import os

PDFS = {
    r"new_task\太妃PA学习pa trainee.pdf": r"new_task\太妃PA学习.txt",
    r"new_task\学委方方土PA—专题课.pdf": r"new_task\学委方方土PA.txt",
}

for pdf_path, out_path in PDFS.items():
    doc = fitz.open(pdf_path)
    print(f"FILE: {os.path.basename(pdf_path)}   pages={len(doc)}")
    parts = []
    for i, page in enumerate(doc):
        text = page.get_text("text")
        parts.append(f"\n========== PDF Page {i+1} / {len(doc)} ==========\n")
        parts.append(text)
    doc.close()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    print(f"  -> {out_path}")
print("DONE")