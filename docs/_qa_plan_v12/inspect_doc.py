from docx import Document

p = r"C:\Users\Lenovo\Desktop\AgenticAI-main\docs\Novelty-Lens-Comprehensive-Development-Plan-EN-v1.1.docx"
d = Document(p)
for i, para in enumerate(d.paragraphs):
    text = para.text.strip()
    if text:
        print(f"{i:04d}\t{para.style.name}\t{text}")
