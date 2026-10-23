from pathlib import Path
from zipfile import ZipFile


def read_links(path):
    path = Path(path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("输入文件超过20MB")
    if path.suffix.lower() in (".txt", ".csv", ".md", ".tsv"):
        return path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() != ".xlsx":
        raise ValueError("支持文本、CSV、TSV和XLSX")
    with ZipFile(path) as z:
        if sum(i.file_size for i in z.infolist()) > 100 * 1024 * 1024:
            raise ValueError("Excel展开大小超过限制")
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=False, data_only=False)
    result = []
    try:
        for sheet in book:
            for row in sheet:
                for cell in row:
                    if cell.hyperlink:
                        result.append(cell.hyperlink.target or "")
                    elif cell.value is not None:
                        result.append(str(cell.value))
    finally:
        book.close()
    return "\n".join(result)
