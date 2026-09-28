from __future__ import annotations

import copy
import shutil
import zipfile
from pathlib import Path

from lxml import etree


SOURCE = Path(r"C:\Users\Lenovo\Desktop\COOP\COOPCV.docx")
OUTPUT = Path(r"C:\Users\Lenovo\Desktop\AgenticAI-main\COOPCV_updated.docx")
WORKING = OUTPUT.with_suffix(".working.docx")
NAMESPACES = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def paragraph_text(paragraph: etree._Element) -> str:
    pieces: list[str] = []
    for item in paragraph.iter():
        if item.tag == W + "t":
            pieces.append(item.text or "")
        elif item.tag == W + "tab":
            pieces.append("\t")
    return "".join(pieces)


def replace_text_nodes(paragraph: etree._Element, values: list[str]) -> None:
    nodes = paragraph.xpath(".//w:t", namespaces=NAMESPACES)
    if len(nodes) < len(values):
        raise ValueError("The copied paragraph does not have the expected text runs.")
    for index, value in enumerate(values):
        nodes[index].text = value
    for node in nodes[len(values) :]:
        node.text = ""


def main() -> None:
    if not SOURCE.is_file():
        raise FileNotFoundError(SOURCE)

    shutil.copy2(SOURCE, WORKING)

    with zipfile.ZipFile(WORKING, "r") as archive:
        parts = {info.filename: archive.read(info.filename) for info in archive.infolist()}

    root = etree.fromstring(parts["word/document.xml"])
    paragraphs = root.xpath(".//w:body/w:p", namespaces=NAMESPACES)
    club_heading = next(
        paragraph
        for paragraph in paragraphs
        if paragraph_text(paragraph).lstrip().startswith("Member")
        and "AI Club, University of Hail" in paragraph_text(paragraph)
    )
    club_index = paragraphs.index(club_heading)
    club_bullet = paragraphs[club_index + 1]
    if "Participated in AI projects" not in paragraph_text(club_bullet):
        raise ValueError("The AI Club bullet was not found immediately after its heading.")

    aramco_heading = copy.deepcopy(club_heading)
    replace_text_nodes(
        aramco_heading,
        ["Co-op Intern — Saudi Aramco", "06/2026 — Present"],
    )

    bullets = [
        "Worked in the Digital & AI Center of Excellence, Autonomous Technologies, on an AI-powered patent search project.",
        "Improved search functionality and supported frontend development for internal AI projects.",
        "Led an Agentic AI hackathon team to build a Scope of Work generator.",
    ]
    aramco_bullets = []
    for text in bullets:
        paragraph = copy.deepcopy(club_bullet)
        replace_text_nodes(paragraph, [text])
        aramco_bullets.append(paragraph)

    club_heading.addprevious(aramco_heading)
    for paragraph in aramco_bullets:
        club_heading.addprevious(paragraph)

    parts["word/document.xml"] = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )

    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, data in parts.items():
            archive.writestr(filename, data)

    WORKING.unlink()
    print(OUTPUT)


if __name__ == "__main__":
    main()
