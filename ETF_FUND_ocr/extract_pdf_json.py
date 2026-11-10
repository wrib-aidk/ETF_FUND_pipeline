"""PDF의 텍스트, 표, 이미지를 JSON으로 추출한다. (OCR 없음)

사용법:
    python extract_pdf_json.py 입력파일 [-o 출력.json] [--images-dir 이미지폴더]

모든 페이지를 PNG로 렌더링한다. OCR/VLM은 ocr_targets의 페이지 파일을 읽어 해당 페이지의 ocr_text를 채운다.
표로 감지된 영역은 위치만 기록하고, 영역 안의 텍스트 줄은 type을 table로 분류한다.
PDF에 포함된 이미지는 파일로 내보내고, 글자가 있을 가능성을 픽셀 모양으로 판정한다.
문서 구조와 쪽 경계 표 연결은 build_doc_structure.py 또는 build_doc_structure_ocr.py에서 처리한다.
이미지 경로는 images_dir의 부모 기준이며, JSON을 같은 부모 폴더에 저장한다.
"""
import argparse
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader
from scipy import ndimage

SCHEMA_VERSION = 4
NUMBER = re.compile(r"^-?\d[\d,]*(\.\d+)?$")
PAGE_RENDER_SCALE = 3  # 페이지 렌더링 해상도 (216 DPI)

# 반복 문구: 위아래 여백에서 전체 페이지의 일정 비율 이상 나타나는 줄
MARGIN_RATIO = 0.12
REPEAT_MIN_PAGES = 3
REPEAT_PAGE_RATIO = 0.4

# 이미지 글자 판정: 작은 덩어리가 가로로 3개 이상 이어지면 글자 줄로 본다
MIN_GLYPH_HEIGHT = 5
MAX_GLYPH_HEIGHT = 60
MIN_CHAIN = 3
SMALL_IMAGE_SIDE = 60


def clean_cell(cell):
    return re.sub(r"\s*\n\s*", " ", cell or "").strip()


def merge_sparse_columns(rows):
    """같은 행에서 값이 겹치지 않는 인접 열을 합친다 (병합 셀로 어긋난 열 보정)."""
    if not rows:
        return rows
    merged = []
    for col in (list(col) for col in zip(*rows)):
        if not any(col):
            continue
        if merged and not any(a and b for a, b in zip(merged[-1], col)):
            merged[-1] = [a or b for a, b in zip(merged[-1], col)]
        else:
            merged.append(col)
    return [list(row) for row in zip(*merged)] if merged else []


def merge_header_rows(rows):
    """첫 숫자 행 앞의 머리글 행들을 한 행으로 합친다."""
    first_data = next(
        (i for i, row in enumerate(rows) if any(NUMBER.match(c) for c in row)), 1
    )
    if first_data <= 1:
        return rows
    header = [
        " ".join(row[i] for row in rows[:first_data] if row[i])
        for i in range(len(rows[0]))
    ]
    return [header] + rows[first_data:]


def normalize_table(raw):
    rows = [[clean_cell(c) for c in row] for row in raw]
    rows = [row for row in rows if any(row)]
    return merge_header_rows(merge_sparse_columns(rows))


def contains(outer, inner):
    return (
        outer[0] <= inner[0] + 1
        and outer[1] <= inner[1] + 1
        and outer[2] >= inner[2] - 1
        and outer[3] >= inner[3] - 1
    )


def fix_upright(page):
    """행렬 값의 미세한 오차로 세로 글자로 잘못 분류된 글자를 바로잡는다."""
    for char in page.chars:
        a, b, c, d = char["matrix"][:4]
        if a > 0 and d > 0 and abs(b) < 0.01 and abs(c) < 0.01:
            char["upright"] = True


def has_collapsed_numbers(raw):
    """여러 행의 숫자가 한 칸에 몰렸는지 확인한다 (행 구분선이 없는 재무제표 등)."""
    for row in raw:
        for cell in row:
            lines = [line.strip() for line in (cell or "").split("\n")]
            if sum(bool(NUMBER.match(line)) for line in lines) >= 4:
                return True
    return False


def leaf_cells(region_box, tables):
    """영역 안의 칸 중 다른 칸을 품지 않는 가장 작은 칸만 남긴다. OCR 줄을 칸 단위로 묶을 때 쓴다."""
    cells = {
        tuple(rounded(cell))
        for table in tables
        if len(table.cells) > 1
        for cell in table.cells
        if cell and contains(region_box, cell)
    }
    leaves = [
        cell for cell in cells
        if not any(other != cell and contains(cell, other) for other in cells)
    ]
    return [list(cell) for cell in sorted(leaves, key=lambda c: (c[1], c[0]))]


def table_regions(page, tables):
    """가장 바깥 표 영역의 위치와 칸 경계를 기록한다. 칸이 하나뿐인 상자는 제외한다."""
    boxes = [table.bbox for table in tables if len(table.cells) > 1]
    outermost = []
    for index, box in enumerate(boxes):
        inside_other = any(
            contains(other, box) and (not contains(box, other) or other_index < index)
            for other_index, other in enumerate(boxes)
            if other_index != index
        )
        if not inside_other:
            outermost.append(box)
    outermost.sort(key=lambda box: (box[1], box[0]))
    return [
        {"id": f"p{page.page_number}_t{index}", "bbox": rounded(box), "cells": leaf_cells(box, tables)}
        for index, box in enumerate(outermost, 1)
    ]


def select_tables(tables):
    """조각 표와 페이지 틀 표를 빼고 실제 데이터 표만 남긴다."""
    found = []
    for table in tables:
        raw = table.extract()
        rows = normalize_table(raw)
        if rows and len(rows[0]) > 1:
            found.append(
                {
                    "bbox": table.bbox,
                    "cells": [cell for cell in table.cells if cell],
                    "rows": rows,
                    "collapsed": has_collapsed_numbers(raw),
                }
            )
    return [
        item
        for item in found
        if not any(
            other is not item and contains(item["bbox"], other["bbox"])
            for other in found
        )
    ]


def layout_lines(page, bbox):
    """열 위치를 공백으로 유지한 줄 목록을 만든다."""
    text = page.crop(bbox).extract_text(layout=True, x_density=4.5) or ""
    lines = [line.rstrip() for line in text.split("\n") if line.strip()]
    indent = min((len(line) - len(line.lstrip()) for line in lines), default=0)
    return [line[indent:] for line in lines]


def rounded(bbox):
    return [round(float(v), 1) for v in bbox]


def text_likeness(image):
    """픽셀 모양으로 글자 줄을 찾는다. 글자를 읽는 것이 아니라 가능성만 판정한다."""
    canvas = Image.new("RGBA", image.size, "white")
    canvas.alpha_composite(image.convert("RGBA"))
    gray = canvas.convert("L")
    if min(gray.size) < SMALL_IMAGE_SIDE:
        gray = gray.resize((gray.width * 3, gray.height * 3), Image.LANCZOS)
    pixels = np.asarray(gray, dtype=float)

    background = ndimage.median_filter(pixels, size=21)
    labels, _ = ndimage.label(np.abs(pixels - background) > 50, structure=np.ones((3, 3)))

    glyphs = []
    for box in ndimage.find_objects(labels):
        height = box[0].stop - box[0].start
        width = box[1].stop - box[1].start
        if MIN_GLYPH_HEIGHT <= height <= MAX_GLYPH_HEIGHT and width <= height * 3:
            glyphs.append((box[1].start, box[1].stop, box[0].start, box[0].stop))
    glyphs.sort()

    parent = list(range(len(glyphs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(glyphs):
        height_a = a[3] - a[2]
        for j in range(i + 1, len(glyphs)):
            b = glyphs[j]
            height_b = b[3] - b[2]
            limit = 2 * max(height_a, height_b)
            if b[0] - a[1] > limit:
                break
            center_gap = abs((a[2] + a[3]) - (b[2] + b[3])) / 2
            if center_gap < 0.5 * max(height_a, height_b):
                parent[find(i)] = find(j)

    chains = Counter(find(i) for i in range(len(glyphs)))
    rows = [size for size in chains.values() if size >= MIN_CHAIN]
    return {
        "method": "pixel_heuristic",
        "likely_has_text": bool(rows),
        "text_rows": len(rows),
        "glyph_candidates": sum(rows),
    }


def extract_images(page, reader_page, images_dir):
    blocks = []
    decoded = reader_page.images  # 꺼낼 때 디코딩하므로 하나가 실패해도 나머지는 계속 처리한다
    area = float(page.width * page.height)
    for index, img in enumerate(page.images, 1):
        image_id = f"p{page.page_number}_i{index}"
        size = list(img.get("srcsize") or [])
        block = {
            "type": "image",
            "id": image_id,
            "page": page.page_number,
            "bbox": rounded((img["x0"], img["top"], img["x1"], img["bottom"])),
            "pixels": [int(v) for v in size],
            "page_coverage_pct": round(
                float((img["x1"] - img["x0"]) * (img["bottom"] - img["top"])) / area * 100, 1
            ),
            "file": None,
            "text_check": None,
        }
        try:
            data = decoded[index - 1]
            suffix = Path(data.name).suffix or ".bin"
            target = images_dir / f"{image_id}{suffix}"
            images_dir.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.data)
            block["file"] = str(target.relative_to(images_dir.parent)).replace("\\", "/")
            block["text_check"] = text_likeness(Image.open(io.BytesIO(data.data)))
        except Exception as error:  # 이미지 하나의 디코딩 실패로 문서 전체를 멈추지 않는다
            block["text_check"] = {"method": "failed", "error": repr(error)}
        blocks.append(block)
    return blocks


def render_page(page_number, pdfium_page, images_dir):
    """OCR/VLM에 넘길 페이지 이미지를 저장하고 JSON 기준 상대 경로를 돌려준다."""
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / f"page_{page_number:03d}.png"
    pdfium_page.render(scale=PAGE_RENDER_SCALE).to_pil().save(target)
    return str(target.relative_to(images_dir.parent)).replace("\\", "/")


def page_blocks(page, reader_page, images_dir):
    """표, 텍스트, 이미지를 위에서 아래 순서의 블록으로 만들고 표 영역 목록을 함께 돌려준다."""
    found = page.find_tables()
    tables = select_tables(found)
    regions = table_regions(page, found)

    def region_of(bbox):
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        for region in regions:
            r = region["bbox"]
            if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]:
                return region["id"]
        return None

    # 칸으로 인식되지 않은 글자가 버려지지 않도록 표 전체가 아니라 칸 단위로 제외한다.
    boxes = []
    for table in tables:
        boxes.extend([table["bbox"]] if table["collapsed"] else table["cells"])

    def outside_tables(obj):
        cx = (obj["x0"] + obj["x1"]) / 2
        cy = (obj["top"] + obj["bottom"]) / 2
        return not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)

    blocks = []
    for table in tables:
        block = {
            "type": "table",
            "page": page.page_number,
            "bbox": rounded(table["bbox"]),
            "region_id": region_of(table["bbox"]),
        }
        if table["collapsed"]:
            block.update(table_format="layout", lines=layout_lines(page, table["bbox"]))
        else:
            block.update(table_format="grid", header=table["rows"][0], rows=table["rows"][1:])
        blocks.append(block)
    for line in page.filter(outside_tables).extract_text_lines(return_chars=False):
        bbox = rounded((line["x0"], line["top"], line["x1"], line["bottom"]))
        block = {"type": "text", "page": page.page_number, "bbox": bbox, "text": line["text"]}
        # 표 영역 안의 줄은 내용을 그대로 두고 분류만 표로 바꾼다.
        region_id = region_of(bbox)
        if region_id:
            block.update(type="table", table_format="line", region_id=region_id)
        blocks.append(block)
    blocks.extend(extract_images(page, reader_page, images_dir))
    blocks.sort(key=lambda block: block["bbox"][1])
    return blocks, regions


def block_strings(block):
    if "text" in block:
        return [block["text"]]
    if "lines" in block:
        return block["lines"]
    if "header" in block:
        return [c for row in [block["header"], *block["rows"]] for c in row]
    return []


def missing_chars(page, blocks):
    """PDF에는 있지만 추출 결과에 빠진 글자 수를 센다."""
    source = Counter(c["text"] for c in page.chars if c["text"].strip())
    extracted = Counter("".join(s for b in blocks for s in block_strings(b)))
    return sum((source - extracted).values())


def remove_repeated_text(pages):
    """여러 페이지의 위아래 여백에 반복되는 줄(문서명, 쪽번호 등)을 본문에서 뺀다."""
    seen = defaultdict(set)

    def key_of(page, block):
        top, bottom = block["bbox"][1], block["bbox"][3]
        if top < page["height"] * MARGIN_RATIO:
            zone = "top"
        elif bottom > page["height"] * (1 - MARGIN_RATIO):
            zone = "bottom"
        else:
            return None
        return zone, re.sub(r"\d+", "#", block["text"]).strip()

    for page in pages:
        for block in page["blocks"]:
            if "text" in block and (key := key_of(page, block)):
                seen[key].add(page["page"])

    threshold = max(REPEAT_MIN_PAGES, len(pages) * REPEAT_PAGE_RATIO)
    repeated = {key for key, found in seen.items() if len(found) >= threshold}
    for page in pages:
        page["blocks"] = [
            block
            for block in page["blocks"]
            if not ("text" in block and key_of(page, block) in repeated)
        ]
    return [
        {"zone": zone, "pattern": pattern, "pages": len(seen[(zone, pattern)])}
        for zone, pattern in sorted(repeated)
    ]


def extract(path, images_dir):
    reader = PdfReader(str(path))
    document = pdfium.PdfDocument(str(path))
    pages = []
    missing = {}
    with pdfplumber.open(path) as pdf:
        for index, (page, reader_page) in enumerate(zip(pdf.pages, reader.pages)):
            fix_upright(page)
            blocks, regions = page_blocks(page, reader_page, images_dir)
            lost = missing_chars(page, blocks)
            if lost:
                missing[str(page.page_number)] = lost
            pages.append(
                {
                    "page": page.page_number,
                    "width": round(float(page.width), 1),
                    "height": round(float(page.height), 1),
                    "file": render_page(page.page_number, document[index], images_dir),
                    "ocr_text": None,
                    "table_regions": regions,
                    "blocks": blocks,
                }
            )

    repeated = remove_repeated_text(pages)
    ocr_targets = [
        {
            "page": page["page"],
            "file": page["file"],
            "has_table_region": bool(page["table_regions"]),
            "has_image_with_text": any(
                block["type"] == "image" and (block["text_check"] or {}).get("likely_has_text")
                for block in page["blocks"]
            ),
        }
        for page in pages
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "file": path.name,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "pages": len(pages),
        },
        "extraction_check": {"missing_chars_by_page": missing},
        "repeated_text": repeated,
        "ocr_targets": ocr_targets,
        "pages": pages,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--images-dir", type=Path)
    args = parser.parse_args()

    stem = re.sub(r"(\.pdf)?(\.raw)?$", "", args.pdf.name, flags=re.I)
    output = args.output or args.pdf.with_name(stem + ".json")
    images_dir = args.images_dir or output.with_name(output.stem + "_images")

    result = extract(args.pdf, images_dir)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"pages={result['source']['pages']} output={output}")
    print("missing chars by page:", result["extraction_check"]["missing_chars_by_page"] or "none")
    print("repeated text:", result["repeated_text"])
    targets = result["ocr_targets"]
    print(
        f"ocr targets: pages={len(targets)} "
        f"with_table_region={sum(t['has_table_region'] for t in targets)} "
        f"with_image_text={sum(t['has_image_with_text'] for t in targets)}"
    )
    for page in result["pages"]:
        for block in page["blocks"]:
            if block["type"] == "image":
                print(block["id"], block["pixels"], block["text_check"])


if __name__ == "__main__":
    main()
