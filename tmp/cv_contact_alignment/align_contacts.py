from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from lxml import etree


SOURCE = Path(r"C:\Users\Lenovo\Desktop\COOP\COOPCV.docx")
OUTPUT = Path(r"C:\Users\Lenovo\Desktop\AgenticAI-main\COOPCV_contact_aligned.docx")

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def qn(local_name: str) -> str:
    return W + local_name


def paragraph_text(paragraph: etree._Element) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def make_tab_run() -> etree._Element:
    run = etree.Element(qn("r"))
    etree.SubElement(run, qn("tab"))
    return run


def raise_text_run(run: etree._Element, half_points: int = 8) -> None:
    run_properties = run.find(qn("rPr"))
    if run_properties is None:
        run_properties = etree.Element(qn("rPr"))
        run.insert(0, run_properties)

    old_position = run_properties.find(qn("position"))
    if old_position is not None:
        run_properties.remove(old_position)

    position = etree.Element(qn("position"))
    position.set(qn("val"), str(half_points))
    size = run_properties.find(qn("sz"))
    if size is None:
        run_properties.append(position)
    else:
        run_properties.insert(run_properties.index(size), position)


def main() -> None:
    if not SOURCE.is_file():
        raise FileNotFoundError(SOURCE)

    with zipfile.ZipFile(SOURCE, "r") as archive:
        source_infos = archive.infolist()
        parts = {info.filename: archive.read(info.filename) for info in source_infos}

    root = etree.fromstring(parts["word/document.xml"])
    paragraphs = root.xpath(".//w:body/w:p", namespaces=NS)
    contact = next(
        paragraph
        for paragraph in paragraphs
        if "ayoubalswida@gmail.com" in paragraph_text(paragraph).lower()
        and "+966 507940271" in paragraph_text(paragraph)
    )

    p_properties = contact.find(qn("pPr"))
    if p_properties is None:
        raise ValueError("The contact paragraph has no paragraph properties.")

    existing_tabs = p_properties.find(qn("tabs"))
    if existing_tabs is not None:
        p_properties.remove(existing_tabs)

    tabs = etree.Element(qn("tabs"))
    for position in (1313, 3939, 6566, 9193):
        tab = etree.SubElement(tabs, qn("tab"))
        tab.set(qn("val"), "center")
        tab.set(qn("pos"), str(position))

    spacing = p_properties.find(qn("spacing"))
    if spacing is None:
        p_properties.insert(0, tabs)
    else:
        p_properties.insert(p_properties.index(spacing), tabs)

    justification = p_properties.find(qn("jc"))
    if justification is None:
        justification = etree.SubElement(p_properties, qn("jc"))
    justification.set(qn("val"), "left")

    for run in list(contact.findall(qn("r"))):
        text = "".join(run.xpath(".//w:t/text()", namespaces=NS))
        has_visual = bool(run.xpath(".//w:drawing | .//w:pict", namespaces=NS))
        if not text.strip() and not has_visual:
            contact.remove(run)

    hyperlinks = contact.findall(qn("hyperlink"))
    if len(hyperlinks) != 5:
        raise ValueError(f"Expected five contact hyperlink elements, found {len(hyperlinks)}.")

    group_starts = (hyperlinks[0], hyperlinks[2], hyperlinks[3], hyperlinks[4])
    for hyperlink in group_starts:
        contact.insert(contact.index(hyperlink), make_tab_run())

    for text_run in contact.xpath("./w:hyperlink/w:r[w:t]", namespaces=NS):
        raise_text_run(text_run)

    parts["word/document.xml"] = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )

    with zipfile.ZipFile(OUTPUT, "w") as archive:
        for info in source_infos:
            archive.writestr(info, parts[info.filename])

    with zipfile.ZipFile(SOURCE, "r") as source_archive, zipfile.ZipFile(OUTPUT, "r") as output_archive:
        source_names = source_archive.namelist()
        output_names = output_archive.namelist()
        if source_names != output_names:
            raise RuntimeError("Package entries changed unexpectedly.")
        changed = [
            name
            for name in source_names
            if hashlib.sha256(source_archive.read(name)).digest()
            != hashlib.sha256(output_archive.read(name)).digest()
        ]
        if changed != ["word/document.xml"]:
            raise RuntimeError(f"Unexpected package parts changed: {changed}")

    print(OUTPUT)


if __name__ == "__main__":
    main()
