"""2층: 투자설명서 PDF를 문서 구조(섹션, 문단, 격자 표, 머리글 경로)로 정리한다.

1층(extract_pdf_json.py 결과)의 반복 문구·이미지 정보와, 있으면 OCR 결과(ocr_pages_server.py)를 함께 쓴다.
지표 추출은 하지 않는다. 값마다 "어느 표의 어느 머리글 아래 값인지"까지만 만든다.

규칙은 문서와 무관한 기준만 쓴다: 칸 경계, 배경색 차이, 숫자 여부, 글자 정렬, 번호 패턴.

사용법:
    python build_doc_structure.py 원본.pdf 추출.json [--ocr OCR.json] [-o 출력.json]
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ETF_FUND_ocr"))  # fix_upright 공유
from extract_pdf_json import fix_upright  # noqa: E402

STRUCTURE_VERSION = 1

NUMERIC = re.compile(r"^[-+]?\(?[\d,]*\.?\d+\)?\s*%?$|^-$")
NOTE = re.compile(r"^\s*(\(\s*주\s*\d*\s*\)|주\s*\d+\s*\)|※|\*\s)")
UNIT = re.compile(r"^[\[(<]?\s*단위\s*[:：]\s*[^\])>]+[\])>]?$")
CAPTION = re.compile(r"^\s*[<\[【〈《].{1,60}[>\]】〉》]\s*$")
TOC_LINE = re.compile(r"(?:[.…·]\s*){2,}\d+\s*$")
SENTENCE_END = re.compile(r"(다|요|음|함|임)\s*[.。]?$|[.!?。]$")
NEW_ITEM = re.compile(r"^(\s*[•‧·※*○□■▶◦-]|\d+[.)]|[가-힣][.)]|\(?주\s*\d*\)|[①-⑳]|제\s*\d+\s*[부장절])")

# 제목 번호 체계. 한국 공시 문서의 일반적인 상하 순서.
HEADING_PATTERNS = [
    ("part", re.compile(r"^제\s*\d+\s*부\s*\S")),
    ("chapter", re.compile(r"^\d{1,2}\.\s*[^\d\s]")),
    ("section", re.compile(r"^[가-하]\.\s*\S")),
    ("paren_number", re.compile(r"^\(\d{1,2}\)\s*\S")),
    ("number_paren", re.compile(r"^\d{1,2}\)\s*\S")),
    ("paren_hangul", re.compile(r"^\([가-하]\)\s*\S")),
]
HEADING_MAX_CHARS = 50

GRID_TOLERANCE = 1.5  # 칸 경계 좌표를 같은 선으로 볼 거리 (pt)
FILL_MIN_SIDE = 3  # 이보다 얇은 사각형은 선으로 보고 배경색에서 뺀다
FILL_COVER = 0.5  # 칸 면적의 이 비율 이상을 덮는 사각형의 색을 칸 배경색으로 본다
CONTINUE_TOLERANCE = 5  # 쪽 경계 표 연속 판단: 열 경계 차이 (pt)
ALIGNED_GAP_RATIO = 1.0  # 선 없는 표: 글자 높이의 이 배수 이상 떨어지면 다른 칸
PARAGRAPH_GAP_RATIO = 0.8  # 문단: 줄 간격이 글자 높이의 이 배수를 넘으면 새 문단
RIGHT_EDGE_RATIO = 0.03  # 문단: 오른쪽 끝에서 페이지 폭의 이 비율 안이면 줄이 꽉 찬 것으로 본다
MARGIN_RATIO = 0.12


def squash(text):
    return re.sub(r"\s+", "", text or "")


def is_numeric(text):
    return bool(NUMERIC.match((text or "").strip()))


def center(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def inside(box, x, y, pad=0.5):
    return box[0] - pad <= x <= box[2] + pad and box[1] - pad <= y <= box[3] + pad


def contains(outer, inner, pad=1):
    return (outer[0] <= inner[0] + pad and outer[1] <= inner[1] + pad
            and outer[2] >= inner[2] - pad and outer[3] >= inner[3] - pad)


def overlap_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def rounded(box):
    return [round(float(v), 1) for v in box]


def obj_center(obj):
    return (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2


def cluster(values, tolerance=GRID_TOLERANCE):
    result = []
    for value in sorted(values):
        if result and value - result[-1][-1] <= tolerance:
            result[-1].append(value)
        else:
            result.append([value])
    return [sum(group) / len(group) for group in result]


def nearest(value, points):
    return min(range(len(points)), key=lambda i: abs(points[i] - value))


def is_white(color):
    if color is None:
        return True
    values = color if isinstance(color, (list, tuple)) else [color]
    try:
        values = [float(v) for v in values]
    except (TypeError, ValueError):  # 패턴 이름 등 숫자가 아닌 색은 흰색으로 보지 않는다
        return False
    if len(values) == 4:  # CMYK
        return all(v <= 0.03 for v in values)
    return all(v >= 0.97 for v in values)


def color_key(color):
    values = color if isinstance(color, (list, tuple)) else [color]
    # 패턴 이름(예: 'P38') 같은 숫자가 아닌 값은 글자 그대로 색 구분 키로 쓴다
    return tuple(round(float(v), 2) if isinstance(v, (int, float)) else str(v) for v in values)


# ---------------------------------------------------------------- 글자 추출


def lines_in(page, box, exclude=()):
    """box 안(글자 중심 기준)의 글자를 줄로 읽는다. exclude 영역 안의 글자는 뺀다."""

    def keep(obj):
        if obj.get("object_type") != "char":
            return False
        x, y = obj_center(obj)
        return inside(box, x, y) and not any(inside(other, x, y) for other in exclude)

    lines = page.filter(keep).extract_text_lines(return_chars=False)
    return [
        {"text": line["text"].strip(), "bbox": rounded((line["x0"], line["top"], line["x1"], line["bottom"]))}
        for line in lines
        if line["text"].strip()
    ]


def ocr_in(ocr_lines, box):
    found = [line for line in ocr_lines if inside(box, *center(line["bbox"]), pad=1)]
    found.sort(key=lambda line: (line["bbox"][1], line["bbox"][0]))
    return found


# ---------------------------------------------------------------- 격자 표


def fills_of(page):
    return [
        (rect, color_key(rect.get("non_stroking_color")))
        for rect in page.rects
        if rect.get("fill") and min(rect["width"], rect["height"]) > FILL_MIN_SIDE
        and not is_white(rect.get("non_stroking_color"))
    ]


def background(box, fills):
    """칸을 가장 많이 덮는 배경색. 없으면 None (흰색)."""
    area = max((box[2] - box[0]) * (box[3] - box[1]), 1e-6)
    best = None
    for rect, color in fills:
        w = min(box[2], rect["x1"]) - max(box[0], rect["x0"])
        h = min(box[3], rect["bottom"]) - max(box[1], rect["top"])
        if w > 0 and h > 0 and w * h / area >= FILL_COVER:
            rect_area = rect["width"] * rect["height"]
            if best is None or rect_area < best[0]:
                best = (rect_area, color)
    return best[1] if best else None


def make_grid(cell_boxes):
    xs = cluster([b[0] for b in cell_boxes] + [b[2] for b in cell_boxes])
    ys = cluster([b[1] for b in cell_boxes] + [b[3] for b in cell_boxes])
    cells = []
    for box in cell_boxes:
        c0, c1 = nearest(box[0], xs), nearest(box[2], xs)
        r0, r1 = nearest(box[1], ys), nearest(box[3], ys)
        if c1 > c0 and r1 > r0:
            cells.append({"r": r0, "c": c0, "rs": r1 - r0, "cs": c1 - c0, "bbox": rounded(box)})
    cells.sort(key=lambda cell: (cell["r"], cell["c"]))
    return cells, len(ys) - 1, len(xs) - 1, xs


def grid_hole(cells, xs, box):
    """안쪽 표가 칸 하나를 꽉 채우면 pdfplumber가 그 바깥 칸을 만들지 않는다. 안쪽 표 중심의 격자 위치에 빈 칸을 만든다."""
    x, y = center(box)
    tops = {cell["r"]: cell["bbox"][1] for cell in cells}
    bottoms = {cell["r"] + cell["rs"]: cell["bbox"][3] for cell in cells}
    edges = sorted({**tops, **bottoms}.items())
    col = next((c for c in range(len(xs) - 1) if xs[c] <= x < xs[c + 1]), None)
    row = next((r for (r, top), (_, bottom) in zip(edges, edges[1:]) if top <= y < bottom), None)
    if col is None or row is None or any(c["r"] <= row < c["r"] + c["rs"] and c["c"] <= col < c["c"] + c["cs"] for c in cells):
        return None
    top, bottom = dict(edges)[row], dict(edges)[row + 1] if row + 1 in dict(edges) else box[3]
    return {"r": row, "c": col, "rs": 1, "cs": 1, "bbox": rounded((xs[col], top, xs[col + 1], bottom))}


def mark_headers(cells):
    """머리글 칸을 정한다. 배경색이 본문과 다르면 머리글, 배경색 정보가 없으면 숫자 여부로 판단한다."""
    filled = [cell for cell in cells if cell["text"]]
    colors = Counter(cell["fill"] for cell in filled)
    numeric_colors = Counter(cell["fill"] for cell in filled if is_numeric(cell["text"]))
    if numeric_colors:
        # 숫자 값 다수가 같은 배경색일 때만 그 색을 본문 색으로 본다 (칸마다 색이 다른 범례 같은 표는 제외)
        body, count = numeric_colors.most_common(1)[0]
        usable = count >= 2 and count * 2 >= sum(numeric_colors.values())
    else:
        # 숫자가 없으면 흰 칸을 본문으로 본다
        body, usable = None, None in colors
    if len(colors) >= 2 and usable:
        # 글자 칸에 쓰인 적 없는 색이 숫자 칸에만 있으면 머리글 색이 아니라 강조 표시다
        label_colors = {cell["fill"] for cell in filled if not is_numeric(cell["text"])}
        for cell in cells:
            cell["header"] = (bool(cell["text"]) and cell["fill"] != body
                              and (cell["fill"] in label_colors or not is_numeric(cell["text"])))
        return "fill"
    # 배경색 정보가 없을 때: 첫 열 밖에 숫자가 처음 나오는 행 전까지를 머리글 행으로,
    # 숫자가 없는 첫 열을 행 머리글로 본다.
    numeric_rows = [cell["r"] for cell in filled if cell["c"] > 0 and is_numeric(cell["text"])]
    if numeric_rows:
        first_body = min(numeric_rows)
    else:
        # 숫자도 배경색도 없는 표: 첫 행이 짧은 글들이면 머리글 행으로 본다
        first_row = [cell for cell in filled if cell["r"] == 0]
        short = first_row and all(len(cell["text"]) <= ROW_LABEL_MAX_CHARS for cell in first_row)
        first_body = 1 if short and any(cell["r"] > 0 for cell in filled) else 0
    first_col = [cell for cell in filled if cell["c"] == 0 and cell["r"] >= first_body]
    col_header = bool(numeric_rows) and first_col and not any(is_numeric(cell["text"]) for cell in first_col)
    for cell in cells:
        top = cell["r"] < first_body and cell["r"] + cell["rs"] <= first_body
        cell["header"] = bool(cell["text"]) and (top or (col_header and cell["c"] == 0))
    return "numeric"


ROW_LABEL_MAX_CHARS = 30


def mark_row_labels(cells):
    """행에서 왼쪽에 숫자(순번)나 빈칸만 있는 첫 번째 짧은 비숫자 칸은, 오른쪽에 숫자나 긴 글이 있으면 행 머리글로 본다.

    배경색이 없는 행 이름 열과, 순번 열 다음에 오는 이름 열을 잡기 위한 규칙이다.
    """
    for cell in cells:
        if cell["header"] or not cell["text"] or is_numeric(cell["text"]):
            continue
        rows = range(cell["r"], cell["r"] + cell["rs"])
        same_row = [o for o in cells if o is not cell and o["text"] and not o["header"] and o["r"] in rows]
        left = [o for o in same_row if o["c"] + o["cs"] <= cell["c"]]
        right = [o for o in same_row if o["c"] >= cell["c"] + cell["cs"]]
        # 오른쪽이 모두 숫자(2칸 이상)인 강한 경우에만, 긴 이름이나 묶음 머리글 아래의 하위 행 이름을 허용한다
        strong = len(right) >= 2 and all(is_numeric(o["text"]) for o in right)
        # 이미 왼쪽에 행 머리글이 있으면 이 칸은 값이다. 단 그 머리글이 더 많은 행에 걸친 묶음 이름이면 예외
        if any(o["header"] and o["text"] and o["r"] in rows and o["c"] + o["cs"] <= cell["c"]
               and (o["rs"] <= cell["rs"] or not strong) for o in cells):
            continue
        if len(cell["text"]) > ROW_LABEL_MAX_CHARS and not strong:
            continue
        if any(not is_numeric(o["text"]) for o in left):
            continue
        if any(is_numeric(o["text"]) or len(o["text"]) > ROW_LABEL_MAX_CHARS for o in right):
            cell["header"] = True


def header_paths(cells):
    """값 칸마다 위쪽 머리글(열 경로)과 왼쪽 머리글(행 경로)을 모은다.

    값 칸은 건너뛰고 머리글을 모으다가, 머리글을 모은 뒤 다시 값 칸을 만나면 멈춘다.
    그래서 같은 표 안의 다른 머리글 묶음까지 넘어가지 않는다.
    같은 열(행)에서 값 칸의 범위 안에 머리글 칸이 여럿일 때, 나뉜 경계선을 다른 열(행)이 쓰지 않으면
    쪼개진 라벨로 보고 이어 붙이고, 쓰면 서로 다른 행(열)이므로 " | "로 구분한다.
    """
    at = {}
    row_edges, col_edges = defaultdict(set), defaultdict(set)
    for index, cell in enumerate(cells):
        for r in range(cell["r"], cell["r"] + cell["rs"]):
            for c in range(cell["c"], cell["c"] + cell["cs"]):
                at[(r, c)] = index
        for c in range(cell["c"], cell["c"] + cell["cs"]):
            row_edges[cell["r"]].add(c)
            row_edges[cell["r"] + cell["rs"]].add(c)
        for r in range(cell["r"], cell["r"] + cell["rs"]):
            col_edges[cell["c"]].add(r)
            col_edges[cell["c"] + cell["cs"]].add(r)

    def walk(cell, axis):
        path = []
        if axis == "up":
            span = range(cell["c"], cell["c"] + cell["cs"])
            steps = range(cell["r"] - 1, -1, -1)
            key = lambda step, k: (step, k)
            # 머리글 칸들이 가로로 놓임: 경계는 열 번호, 그 경계를 쓰는 행 집합을 본다
            start, other, other_size, edges = "c", "r", "rs", col_edges
        else:
            span = range(cell["r"], cell["r"] + cell["rs"])
            steps = range(cell["c"] - 1, -1, -1)
            key = lambda step, k: (k, step)
            # 머리글 칸들이 세로로 놓임: 경계는 행 번호, 그 경계를 쓰는 열 집합을 본다
            start, other, other_size, edges = "r", "c", "cs", row_edges
        seen = set()
        for step in steps:
            found = []
            for k in span:
                index = at.get(key(step, k))
                if index is not None and index not in found and index not in seen:
                    found.append(index)
            if not found:
                continue
            seen.update(found)
            if any(not cells[i]["header"] and cells[i]["text"] for i in found):
                if path:
                    break
                continue
            parts = [cells[i] for i in found if cells[i]["text"]]
            if not parts:
                continue
            text = parts[0]["text"]
            for prev, part in zip(parts, parts[1:]):
                # 두 머리글 칸 사이 경계선을 이 머리글 줄 밖의 칸도 쓰면 진짜 다른 행(열)이다
                own = set(range(part[other], part[other] + part[other_size])) | set(
                    range(prev[other], prev[other] + prev[other_size]))
                genuine = bool(edges[part[start]] - own)
                text += (" | " if genuine else " ") + part["text"]
            path.append(text)
        return list(reversed(path))

    for cell in cells:
        if not cell["header"]:
            cell["col_path"] = walk(cell, "up")
            cell["row_path"] = walk(cell, "left")


def split_notes(lines):
    """줄 목록을 (주N)·※ 로 시작하는 주석 묶음과 나머지로 나눈다."""
    notes, rest, current = [], [], None
    for line in lines:
        if NOTE.match(line["text"]):
            current = {"text": line["text"], "bbox": list(line["bbox"])}
            notes.append(current)
        elif current is not None and line["bbox"][0] > current["bbox"][0]:  # 들여 쓴 이어지는 줄
            current["text"] += " " + line["text"]
            current["bbox"] = [min(current["bbox"][0], line["bbox"][0]), current["bbox"][1],
                               max(current["bbox"][2], line["bbox"][2]), line["bbox"][3]]
        else:
            current = None
            rest.append(line)
    return notes, rest


THIN_LINE = 1.5  # 이보다 얇은 도형(채운 사각형·곡선)은 표 선으로 본다
COLLAPSED_MIN_LINES = 3  # 한 칸에 숫자 줄이 이만큼 이상이면 여러 행이 한 칸에 몰린 것으로 본다


def visible(obj):
    """선·사각형·곡선이 화면에 보이는지. 흰 선과 테두리 없는 흰 채움은 보이지 않는 틀로 본다."""
    if obj.get("object_type") not in ("line", "rect", "curve"):
        return True
    stroked = obj.get("stroke") and not is_white(obj.get("stroking_color"))
    filled = obj.get("fill") and not is_white(obj.get("non_stroking_color"))
    return bool(stroked or filled)


def thin_shapes_as_lines(page):
    """얇은 채움 도형으로 그린 표 선을 선 객체로 바꾼다. pdfplumber 기본 설정은 이런 선을 표 경계로 쓰지 않는다."""
    vertical, horizontal = [], []
    for shape in page.curves + page.rects:
        width, height = shape["x1"] - shape["x0"], shape["bottom"] - shape["top"]
        if width < THIN_LINE and height > FILL_MIN_SIDE:
            x = (shape["x0"] + shape["x1"]) / 2
            vertical.append({"x0": x, "x1": x, "top": shape["top"], "bottom": shape["bottom"],
                             "width": 0, "height": height, "object_type": "line"})
        elif height < THIN_LINE and width > FILL_MIN_SIDE:
            y = (shape["top"] + shape["bottom"]) / 2
            horizontal.append({"x0": shape["x0"], "x1": shape["x1"], "top": y, "bottom": y,
                               "width": width, "height": 0, "object_type": "line"})
    return vertical, horizontal


def text_columns(page, table):
    """글자가 들어 있는 칸들의 열 위치 집합."""
    chars = [obj_center(c) for c in page.chars if c["text"].strip()]
    columns = set()
    for box in table.cells:
        if box and any(inside(box, x, y) for x, y in chars):
            columns.add((round(box[0]), round(box[2])))
    return columns


def regrid_collapsed(cells):
    """여러 행이 한 칸에 몰린 표를 칸 안 줄 높이로 다시 나눈다 (행 구분선이 없는 재무제표 등)."""
    stacked = [c for c in cells if len(c["_lines"]) >= COLLAPSED_MIN_LINES
               and sum(is_numeric(l["text"]) for l in c["_lines"]) >= 2]
    if len({c["c"] for c in stacked}) < 2:
        return cells, None
    lines = [l for c in cells for l in c["_lines"]]
    height = sorted(l["bbox"][3] - l["bbox"][1] for l in lines)[len(lines) // 2]
    rows = cluster([center(l["bbox"])[1] for l in lines], tolerance=height * 0.6)

    def row_of(line):
        return nearest(center(line["bbox"])[1], rows)

    result = []
    for cell in cells:
        if len(cell["_lines"]) >= 2:
            by_row = defaultdict(list)  # 같은 행에 걸린 줄(줄바꿈된 라벨)은 한 칸으로 합친다
            for line in cell["_lines"]:
                by_row[row_of(line)].append(line)
            for row, group in by_row.items():
                box = [min(l["bbox"][0] for l in group), min(l["bbox"][1] for l in group),
                       max(l["bbox"][2] for l in group), max(l["bbox"][3] for l in group)]
                texts = [l["text"] for l in group]
                piece = dict(cell, r=row, rs=1, bbox=box, _lines=group, lines=texts, text=" ".join(texts))
                piece.pop("ocr_match", None)  # 원래 칸 전체에 대한 비교 결과라 나뉜 칸에는 맞지 않는다
                piece.pop("ocr_text", None)
                result.append(piece)
        elif cell["_lines"]:
            covered = [i for i, y in enumerate(rows) if cell["bbox"][1] <= y <= cell["bbox"][3]] or [row_of(cell["_lines"][0])]
            result.append(dict(cell, r=covered[0], rs=covered[-1] - covered[0] + 1))
        else:
            result.append(dict(cell, r=nearest(cell["bbox"][1], rows), rs=1))
    return without_overlaps(result), len(rows)


def without_overlaps(cells):
    """다시 나눈 격자에서 겹치는 칸을 정리한다. 빈 칸이 겹치면 버리고, 위 칸이 아래 칸 자리까지 걸치면 줄인다."""
    cells = sorted(cells, key=lambda c: (not c["text"], c["r"], c["c"]))  # 글자 있는 칸을 먼저 놓는다
    placed, owner = [], {}
    for cell in cells:
        spots = [(r, c) for r in range(cell["r"], cell["r"] + cell["rs"]) for c in range(cell["c"], cell["c"] + cell["cs"])]
        clashes = {id(owner[s]): owner[s] for s in spots if s in owner}
        if clashes and not cell["text"]:
            continue
        for other in clashes.values():
            if other["r"] < cell["r"]:  # 위에서 걸쳐 내려온 칸은 이 칸 위까지로 줄인다
                for r in range(cell["r"], other["r"] + other["rs"]):
                    for c in range(other["c"], other["c"] + other["cs"]):
                        owner.pop((r, c), None)
                other["rs"] = cell["r"] - other["r"]
            else:  # 같은 행에서 시작하면 나중 칸이 위 칸 아래로 밀린 경우라 이 칸을 줄인다
                spots = [s for s in spots if s not in owner]
        if not spots:
            # 놓을 자리가 없으면 글자를 버리지 않고 그 자리의 칸에 이어 붙인다
            holder = next(iter(clashes.values()))
            holder["lines"] = holder.get("lines", []) + cell.get("lines", [cell["text"]])
            holder["text"] = " ".join(holder["lines"])
            continue
        cell["r"] = min(r for r, _ in spots)
        cell["rs"] = max(r for r, _ in spots) - cell["r"] + 1
        for s in spots:
            owner[s] = cell
        placed.append(cell)
    placed.sort(key=lambda c: (c["r"], c["c"]))
    return placed


def recover_ruled_holes(boxes, vertical, horizontal, has_text):
    """가로선 일부가 없는 칸을 복구한다. 기존 격자 안에서 양쪽 세로선이 이어진 빈 영역만 쓴다."""
    xs = cluster([b[i] for b in boxes for i in (0, 2)])
    ys = cluster([b[i] for b in boxes for i in (1, 3)])

    def ruled(x, top, bottom):
        spans = sorted((e['top'], e['bottom']) for e in vertical if abs(e['x0'] - x) <= 3)
        end = top
        for a, b in spans:
            if a <= end + 3 and b >= end:
                end = max(end, b)
        return end >= bottom - 3

    def cross(x0, x1, y):
        spans = sorted((e['x0'], e['x1']) for e in horizontal if abs(e['top'] - y) <= 3)
        end = x0
        for a, b in spans:
            if a <= end + 3 and b >= end:
                end = max(end, b)
        return end >= x1 - 3

    holes = []
    for top, bottom in zip(ys, ys[1:]):
        start = None
        for i in range(len(xs)):
            empty = (i < len(xs) - 1 and not any(inside(b, (xs[i] + xs[i + 1]) / 2,
                                                                       (top + bottom) / 2, pad=0) for b in boxes))
            if empty and start is None:
                start = xs[i]
            if not empty and start is not None:
                box = (start, top, xs[i], bottom)
                prior = next((b for b in holes if b[0] == start and b[2] == xs[i]
                              and b[3] == top and not cross(start, xs[i], top)), None)
                if prior:
                    holes.remove(prior)
                    box = (start, prior[1], xs[i], bottom)
                holes.append(box)
                start = None
    return boxes + [b for b in holes if has_text(b) and ruled(b[0], b[1], b[3])
                    and ruled(b[2], b[1], b[3]) and (cross(b[0], b[2], b[1]) or cross(b[0], b[2], b[3]))]


def ruled_tables(page, page_number, fills, ocr_lines, counter):
    """선이 있는 표를 격자로 만든다.

    다른 칸 안의 1열짜리 표는 표가 아니라 칸 글자로 보고, 글자가 한 열에만 있는 표는 글 상자로 보고 뺀다.
    """
    # 보이지 않는 선(흰 선, 흰 채움)은 표 경계에서 뺀다. 문서 배치용 틀이 표로 잡히는 것을 막는다.
    drawn = page.filter(visible)
    vertical, horizontal = thin_shapes_as_lines(drawn)
    settings = {"explicit_vertical_lines": vertical, "explicit_horizontal_lines": horizontal}
    found = [t for t in drawn.find_tables(settings) if len(t.cells) > 1]
    boxes = [tuple(t.bbox) for t in found]

    def parent_of(index):
        holders = [j for j, other in enumerate(boxes)
                   if j != index and contains(other, boxes[index]) and not contains(boxes[index], other)]
        return min(holders, key=lambda j: (boxes[j][2] - boxes[j][0]) * (boxes[j][3] - boxes[j][1])) if holders else None

    kept = [i for i, t in enumerate(found) if len(t.columns) > 1 and len(text_columns(page, t)) > 1]
    tables = {}
    for i in kept:
        counter["table"] += 1
        tables[i] = {"id": f"t{counter['table']}", "type": "table", "kind": "ruled", "page": page_number,
                     "bbox": rounded(boxes[i]), "source_index": i}
    # 부모는 유지된 표 중에서 찾는다
    for i in kept:
        parent = parent_of(i)
        while parent is not None and parent not in tables:
            parent = parent_of(parent)
        tables[i]["parent_index"] = parent

    for i in kept:
        table = tables[i]
        cell_boxes = recover_ruled_holes([tuple(b) for b in found[i].cells if b],
                                        vertical, horizontal, lambda box: bool(lines_in(page, box)))
        cells, n_rows, n_cols, xs = make_grid(cell_boxes)
        children = [tables[j] for j in kept if tables[j]["parent_index"] == i]
        # 안쪽 표는 가장 많이 겹치는 바깥 칸에 붙인다 (여러 칸에 걸친 경우도 있다)
        for child in children:
            if not any(overlap_area(cell["bbox"], child["bbox"]) for cell in cells):
                hole = grid_hole(cells, xs, child["bbox"])
                if hole:
                    cells.append(hole)
        cells.sort(key=lambda cell: (cell["r"], cell["c"]))
        holder_of = {}
        for child in children:
            best = max(range(len(cells)), key=lambda k: overlap_area(cells[k]["bbox"], child["bbox"]), default=None)
            if best is not None:
                holder_of.setdefault(best, []).append(child["id"])
        for k, cell in enumerate(cells):
            # 겹치는 안쪽 표 영역의 글자는 모두 뺀다 (안쪽 표가 따로 가진다)
            inner = [child["bbox"] for child in children if overlap_area(cell["bbox"], child["bbox"]) > 0]
            lines = lines_in(page, cell["bbox"], exclude=inner)
            cell["child_tables"] = holder_of.get(k, [])
            if cell["child_tables"]:
                notes, lines = split_notes(lines)
                cell["notes"] = notes
            cell["_lines"] = lines
            cell["lines"] = [line["text"] for line in lines]
            cell["text"] = " ".join(cell["lines"])
            cell["source"] = "pdf_text" if cell["text"] else None
            ocr = ocr_in(ocr_lines, cell["bbox"]) if ocr_lines is not None else []
            ocr = [line for line in ocr if not any(inside(b, *center(line["bbox"])) for b in inner)]
            if ocr_lines is not None:
                ocr_text = " ".join(line["text"] for line in ocr)
                if not cell["text"] and ocr_text and not cell["child_tables"]:
                    cell.update(text=ocr_text, lines=[line["text"] for line in ocr], source="ocr",
                                ocr_score=round(min(line["score"] for line in ocr), 4))
                elif cell["text"]:
                    cell["ocr_match"] = squash(ocr_text) == squash(cell["text"])
                    if not cell["ocr_match"]:
                        cell["ocr_text"] = ocr_text
            cell["fill"] = background(cell["bbox"], fills)
        cells, regridded_rows = regrid_collapsed(cells)
        if regridded_rows:
            n_rows = regridded_rows
            table["regridded"] = True
        for cell in cells:
            cell.pop("_lines", None)
        table.update(n_rows=n_rows, n_cols=n_cols, col_edges=[round(x, 1) for x in xs])
        table["header_rule"] = mark_headers(cells)
        mark_row_labels(cells)
        header_paths(cells)
        table["cells"] = cells
    return [tables[i] for i in kept]


def link_nested(tables):
    """안쪽 표에 바깥 칸의 머리글 경로(context_path)와 바깥 칸의 주석을 붙인다."""
    by_index = {t["source_index"]: t for t in tables}
    for table in tables:
        table.setdefault("notes", [])
        parent = by_index.get(table.pop("parent_index", None))
        if parent is None:
            table["parent"] = None
            continue
        holder = next(cell for cell in parent["cells"] if table["id"] in cell.get("child_tables", []))
        table["parent"] = {"table": parent["id"], "r": holder["r"], "c": holder["c"]}
        table["context_path"] = holder.get("row_path", []) + holder.get("col_path", [])
        siblings = [t for t in tables if t["id"] in holder["child_tables"]]
        for note in holder.get("notes", []):
            # 주석은 바로 위에 있는 안쪽 표에 붙인다
            above = [t for t in siblings if t["bbox"][3] <= note["bbox"][1] + 2]
            target = max(above, key=lambda t: t["bbox"][3]) if above else siblings[0]
            if target is table:
                table["notes"].append(note)
    for table in tables:
        table.pop("source_index", None)
        table.setdefault("context_path", [])
        for cell in table["cells"]:
            cell.pop("notes", None)
            if not cell["child_tables"]:
                cell.pop("child_tables")


# ---------------------------------------------------------------- 선 없는 표


def segments_of(line, words):
    """한 줄의 단어를 큰 간격 기준으로 칸 후보로 나눈다."""
    members = sorted((w for w in words if inside(line["bbox"], *obj_center(w), pad=1)), key=lambda w: w["x0"])
    if not members:
        return []
    height = max(w["bottom"] - w["top"] for w in members)
    groups = [[members[0]]]
    for word in members[1:]:
        if word["x0"] - groups[-1][-1]["x1"] > height * ALIGNED_GAP_RATIO:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [
        {"text": " ".join(w["text"] for w in group),
         "bbox": rounded((group[0]["x0"], min(w["top"] for w in group), group[-1]["x1"], max(w["bottom"] for w in group)))}
        for group in groups
    ]


def aligned_tables(page, page_number, lines, counter, segs=None, source="pdf_text"):
    """숫자 칸이 여러 개인 줄이 이어지고 칸 위치가 세로로 맞는 부분을 선 없는 표로 만든다.

    segs를 주면(줄마다 칸 후보 목록) PDF 단어 대신 그것을 쓴다. OCR 모드에서 쓴다.
    """
    if segs is None:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
        segs = [segments_of(line, words) for line in lines]
    numeric_rich = [sum(is_numeric(s["text"]) or bool(re.fullmatch(r"[~\-–]?\s*[\d.,\-~ ]+", s["text"])) for s in seg) >= 2
                    for seg in segs]
    used, tables = set(), []
    i = 0
    while i < len(lines):
        if not (len(segs[i]) >= 3 and numeric_rich[i]):
            i += 1
            continue
        # 숫자 줄에서 위아래로 넓힌다. 칸이 2개 이상인 줄과, 그 사이에 낀 1칸 줄을 포함한다.
        start = end = i
        height = lines[i]["bbox"][3] - lines[i]["bbox"][1]

        def near(a, b):
            return 0 <= lines[b]["bbox"][1] - lines[a]["bbox"][3] <= height * 2.5

        while start > 0 and start - 1 not in used and near(start - 1, start) and (
            len(segs[start - 1]) >= 2 or (start > 1 and len(segs[start - 2]) >= 2 and near(start - 2, start - 1))
        ) and not NOTE.match(lines[start - 1]["text"]) and not UNIT.match(lines[start - 1]["text"]):
            start -= 1
        while end + 1 < len(lines) and near(end, end + 1) and len(segs[end + 1]) >= 2 and not NOTE.match(lines[end + 1]["text"]):
            end += 1
        rows = list(range(start, end + 1))
        if sum(numeric_rich[r] for r in rows) < 2:
            i = end + 1
            continue
        # 열 띠: 모든 칸의 가로 구간을 겹치는 것끼리 합친다
        intervals = sorted((s["bbox"][0], s["bbox"][2]) for r in rows for s in segs[r])
        bands = []
        for x0, x1 in intervals:
            if bands and x0 <= bands[-1][1]:
                bands[-1][1] = max(bands[-1][1], x1)
            else:
                bands.append([x0, x1])
        if len(bands) < 3:
            i = end + 1
            continue
        cells = []
        for row_index, r in enumerate(rows):
            for seg in segs[r]:
                covered = [k for k, band in enumerate(bands) if seg["bbox"][0] < band[1] and seg["bbox"][2] > band[0]]
                cells.append({"r": row_index, "c": covered[0], "rs": 1, "cs": len(covered), "bbox": seg["bbox"],
                              "text": seg["text"], "lines": [seg["text"]], "source": source, "fill": None})
        counter["table"] += 1
        table = {"id": f"t{counter['table']}", "type": "table", "kind": "aligned", "page": page_number,
                 "bbox": rounded((min(c["bbox"][0] for c in cells), lines[start]["bbox"][1],
                                  max(c["bbox"][2] for c in cells), lines[end]["bbox"][3])),
                 "n_rows": len(rows), "n_cols": len(bands), "col_edges": [round(b[0], 1) for b in bands] + [round(bands[-1][1], 1)],
                 "parent": None, "context_path": [], "notes": []}
        mark_aligned_headers(cells)
        table["header_rule"] = "aligned"
        header_paths(cells)
        table["cells"] = cells
        tables.append(table)
        used.update(rows)
        i = end + 1
    return tables, used


def mark_aligned_headers(cells):
    """선 없는 표: 첫 열에 이름이 있고 숫자 값이 있는 첫 행 전까지를 머리글 행, 첫 열을 행 머리글로 본다."""
    rows = defaultdict(list)
    for cell in cells:
        rows[cell["r"]].append(cell)
    first_body = next(
        (r for r in sorted(rows)
         if any(c["c"] == 0 and not is_numeric(c["text"]) for c in rows[r])
         and any(c["c"] > 0 and is_numeric(c["text"]) for c in rows[r])),
        0,
    )
    for cell in cells:
        cell["header"] = cell["r"] < first_body or (cell["c"] == 0 and not is_numeric(cell["text"]))


# ---------------------------------------------------------------- 문단과 제목


def repeated_keys(layer1):
    return {(item["zone"], item["pattern"]) for item in layer1.get("repeated_text", [])}


def is_repeated(line, page_height, repeated):
    top, bottom = line["bbox"][1], line["bbox"][3]
    if top < page_height * MARGIN_RATIO:
        zone = "top"
    elif bottom > page_height * (1 - MARGIN_RATIO):
        zone = "bottom"
    else:
        return False
    return (zone, re.sub(r"\d+", "#", line["text"]).strip()) in repeated


def heading_kind(text):
    if len(text) > HEADING_MAX_CHARS or TOC_LINE.search(text) or SENTENCE_END.search(text.rstrip()):
        return None
    if re.search(r"[:：]\s*\S", text):  # "3. 지급시기: 변경일 전일" 처럼 항목: 내용 형태는 목록이다
        return None
    for kind, pattern in HEADING_PATTERNS:
        if pattern.match(text):
            return kind
    return None


def paragraphs(lines, page_width, counter, page_number, text_right, toc_page=False):
    """줄을 문단으로 묶는다. 제목·새 항목·큰 줄 간격·짧게 끝난 줄에서 끊는다.

    text_right는 문서 전체의 본문 오른쪽 끝이다. 쪽마다 구하면 글이 적은 쪽에서 제목이 가장 긴 줄이 되어 버린다.
    toc_page이면 모든 줄을 목차 항목으로 만든다 (쪽 번호가 없는 목차).
    """
    nodes = []
    right = text_right - page_width * RIGHT_EDGE_RATIO
    current = None
    joined = False  # 앞 줄이 내어쓰기로 이어지는 번호 항목이었는지

    def hanging(index):
        """다음 줄이 보통 줄 간격으로 붙고 번호 뒤 글자 위치로 들여 쓰여 있으면, 이 줄은 제목이 아니라 여러 줄 항목의 첫 줄이다."""
        if index + 1 >= len(lines):
            return False
        line, after = lines[index]["bbox"], lines[index + 1]
        height = line[3] - line[1]
        indent = after["bbox"][0] - line[0]
        return (after["bbox"][1] - line[3] <= height * PARAGRAPH_GAP_RATIO
                and height * HANGING_INDENT_RATIO < indent <= height * HANGING_INDENT_MAX
                and heading_kind(after["text"]) is None and not NEW_ITEM.match(after["text"])
                and not UNIT.match(after["text"]) and not CAPTION.match(after["text"]))

    for index, line in enumerate(lines):
        text = line["text"]
        kind = heading_kind(text)
        continued, joined = joined, False
        if kind and hanging(index):
            kind, joined = None, True
        if toc_page and not TOC_LINE.search(text):
            # 점선 없이 따로 정렬된 쪽 번호도 목차 안에서만 분리한다.
            match = re.search(r"\s+(\d+)\s*$", text)
            title = text[:match.start()].strip() if match else text.strip()
            target = int(match.group(1)) if match and heading_kind(title) else None
            counter["node"] += 1
            nodes.append({"id": f"n{counter['node']}", "type": "toc_entry", "page": page_number, "bbox": line["bbox"],
                          "text": title if target is not None else text.strip(), "target_page": target, "raw": text})
            current = None
            continue
        if TOC_LINE.search(text):
            counter["node"] += 1
            match = re.search(r"(\d+)\s*$", text)
            nodes.append({"id": f"n{counter['node']}", "type": "toc_entry", "page": page_number, "bbox": line["bbox"],
                          "text": TOC_LINE.sub("", text).strip(), "target_page": int(match.group(1)), "raw": text})
            current = None
            continue
        if kind and line["bbox"][2] < right:  # 오른쪽 끝까지 찬 줄은 다음 줄로 이어지는 문장이다
            counter["node"] += 1
            nodes.append({"id": f"n{counter['node']}", "type": "heading", "kind": kind, "page": page_number,
                          "bbox": line["bbox"], "text": text})
            current = None
            continue
        height = line["bbox"][3] - line["bbox"][1]
        starts_new = current is None or joined or not continued and (
            NEW_ITEM.match(text)
            or line["bbox"][1] - current["bbox"][3] > height * PARAGRAPH_GAP_RATIO
            or current["last_right"] < right
            or UNIT.match(text) or CAPTION.match(text) or UNIT.match(current["lines"][-1])
            or CAPTION.match(current["lines"][-1])
        )
        if starts_new:
            counter["node"] += 1
            current = {"id": f"n{counter['node']}", "type": "paragraph", "page": page_number,
                       "bbox": list(line["bbox"]), "lines": [text]}
            nodes.append(current)
        else:
            current["lines"].append(text)
            b = current["bbox"]
            current["bbox"] = [min(b[0], line["bbox"][0]), b[1], max(b[2], line["bbox"][2]), line["bbox"][3]]
        current["last_right"] = line["bbox"][2]
    for node in nodes:
        node.pop("last_right", None)
        if node["type"] == "paragraph":
            node["text"] = " ".join(node["lines"])
    return nodes


def attach_captions(nodes):
    """표 바로 위의 단위·제목 줄과 표 바로 뒤의 주석 문단을 표에 붙인다."""
    result = []
    for node in nodes:
        if node["type"] == "table" and node.get("parent") is None:
            while result and result[-1]["type"] == "paragraph" and result[-1]["page"] == node["page"] and (
                UNIT.match(result[-1]["text"]) or CAPTION.match(result[-1]["text"])
            ) and node["bbox"][1] - result[-1]["bbox"][3] < 40:
                previous = result.pop()
                key = "unit" if UNIT.match(previous["text"]) else "caption"
                node.setdefault(key, previous["text"])
        if node["type"] == "paragraph" and NOTE.match(node["text"]):
            # 바로 앞의 (안쪽 표가 아닌) 노드가 표면 그 표의 주석이다
            table = next((n for n in reversed(result) if not (n["type"] == "table" and n.get("parent"))), None)
            if table is not None and table["type"] == "table" and table.get("parent") is None:
                table["notes"].append({"text": node["text"], "bbox": node["bbox"], "page": node["page"]})
                continue
        result.append(node)
    return result


TOC_PAGE_SLACK = 2  # 목차의 쪽 번호와 실제 PDF 쪽이 어긋날 수 있는 범위


TOC_PAGE_HEADING_RATIO = 0.6  # 쪽 번호 없는 목차: 줄의 이 비율 이상이 제목 모양이면 목차 쪽
TOC_PAGE_MIN_LINES = 10
TOC_PAGE_FRONT = 0.2  # 목차는 문서 앞쪽 이 비율 안에서만 찾는다
TOC_TITLE_SIMILARITY = 0.8  # 본문 제목이 목차 제목과 이만큼 비슷하면 같은 제목으로 본다
HANGING_INDENT_RATIO = 0.5  # 다음 줄이 글자 높이의 이 배 넘게 들여 쓰였으면 내어쓰기로 이어지는 줄
HANGING_INDENT_MAX = 3  # 번호 폭 정도까지만. 그보다 멀면 오른쪽 정렬된 단위 줄 같은 다른 글이다


def is_toc_page(lines, page_number, page_count, previous_toc=False):
    """목차 제목, 제목 밀도, 바로 앞 목차의 연속 여부로 판별한다."""
    if any(squash(line["text"]).strip("<>〈〉《》[]【】:：") == "목차" for line in lines):
        return True
    if not lines:
        return False
    # 쪽 번호만 있는 줄은 제목 밀도를 낮추지 않는다.
    lines = [line for line in lines if not re.fullmatch(r"[-–—\s]*\d+[-–—\s]*", line["text"])]
    if not lines:
        return False
    headings = sum(heading_kind(line["text"]) is not None or bool(TOC_LINE.search(line["text"])) for line in lines)
    # 짧은 마지막 목차 쪽만 완화하고 본문 첫 부의 시작은 제외한다.
    if (previous_toc and 3 <= headings and len(lines) < TOC_PAGE_MIN_LINES
            and headings / len(lines) >= 0.8
            and not any(re.match(r"^제\s*1\s*부", line["text"]) for line in lines)):
        return True
    if page_number > max(1, page_count * TOC_PAGE_FRONT) or len(lines) < TOC_PAGE_MIN_LINES:
        return False
    return headings >= TOC_PAGE_MIN_LINES and headings / len(lines) >= TOC_PAGE_HEADING_RATIO


def recover_toc_headings(nodes, counter):
    """목차 항목을 순서대로 본문에서 찾아, 그 제목으로 시작하는 문단을 제목으로 바꾼다.

    "5. 인수에 관한 사항   해당사항 없음"처럼 제목과 내용이 한 줄에 있는 경우를 잡는다. 남는 글은 문단으로 둔다.
    목차에 쪽 번호가 있으면 그 근처에서, 없으면 앞 항목을 찾은 위치 뒤에서 찾는다.
    """
    entries = [n for n in nodes if n["type"] == "toc_entry"]
    result = list(nodes)
    body_start = max((i for i, n in enumerate(result) if n["type"] == "toc_entry"), default=-1) + 1
    pointer = body_start
    for entry in entries:
        title = squash(entry["text"])
        kind = heading_kind(entry["text"])
        if not title or kind is None:
            continue
        for index in range(pointer, len(result)):
            node = result[index]
            if entry.get("target_page") is not None and abs(node["page"] - entry["target_page"]) > TOC_PAGE_SLACK:
                continue
            if node["type"] == "heading" and squash(node["text"]) == title:
                pointer = index + 1
                break
            # 테두리 안에 들어간 부 제목도 목차와 일치하면 복구한다.
            if node["type"] == "table" and kind == "part":
                cell = next((c for c in node["cells"] if squash(c["text"]) == title), None)
                if cell is not None:
                    counter["node"] += 1
                    result.insert(index, {"id": f"n{counter['node']}", "type": "heading", "kind": kind,
                                          "page": node["page"], "bbox": cell["bbox"],
                                          "text": cell["text"], "from_toc": True})
                    pointer = index + 1
                    break
            if node["type"] != "paragraph":
                continue
            first = node["lines"][0]
            if kind == "part":
                first = re.sub(r"^[_\s]+(?=제\s*\d+\s*부)", "", first)
            if not squash(first).startswith(title):
                continue
            pointer = index + 1
            # 제목 글자 수만큼 앞부분을 제목으로, 나머지를 문단으로
            taken, cut = 0, 0
            for cut, char in enumerate(first):
                if taken == len(title):
                    break
                taken += not char.isspace()
            else:
                cut = len(first)
            rest = " ".join([first[cut:].strip(), *node["lines"][1:]]).strip()
            counter["node"] += 1
            heading = {"id": f"n{counter['node']}", "type": "heading", "kind": kind, "page": node["page"],
                       "bbox": node["bbox"], "text": first[:cut].strip(), "from_toc": True}
            replacement = [heading]
            if rest:
                replacement.append(dict(node, lines=[rest], text=rest))
            result[index:index + 1] = replacement
            break
    return result


HANGUL_ORDER = "가나다라마바사아자차카타파하"


def heading_number(text):
    match = re.match(r"^\(?\s*(?:제\s*)?(\d+|[가-하])", text)
    if not match:
        return None
    token = match.group(1)
    return int(token) if token.isdigit() else HANGUL_ORDER.index(token) + 1 if token in HANGUL_ORDER else None


def build_sections(nodes):
    """제목 번호 종류의 상하 순서로 섹션 트리를 만들고, 각 노드에 소속 섹션을 붙인다.

    같은 부모 아래 같은 번호 체계에서 번호가 앞 번호보다 크지 않으면(예: 8. 다음 1.) 제목이 아니라
    본문의 번호 목록으로 보고 문단으로 돌린다.
    """
    order = {kind: rank for rank, (kind, _) in enumerate(HEADING_PATTERNS)}
    # 목차에 나오는 번호 종류는 목차 제목과 비슷한 것만 제목으로 인정한다 (목차와 본문 문구가 조금 다른 문서가 있다)
    toc_titles = defaultdict(set)
    for node in nodes:
        if node["type"] == "toc_entry" and (kind := heading_kind(node["text"])):
            toc_titles[kind].add(squash(node["text"]))

    def in_toc(text, kind):
        title = squash(text)
        return any(SequenceMatcher(None, title, t).ratio() >= TOC_TITLE_SIMILARITY for t in toc_titles[kind])

    def displaced(index):
        """목차와 다른 제목 뒤, 같은 상위 섹션 안에 목차와 맞는 같은 번호 제목이 따로 있으면 앞의 것은 목록 항목이다."""
        node = nodes[index]
        kind, number = node["kind"], heading_number(node["text"])
        if in_toc(node["text"], kind):
            return False
        for other in nodes[index + 1:]:
            if other["type"] == "heading" and order[other["kind"]] < order[kind]:
                return False
            text = other.get("text") if other["type"] == "heading" else (other.get("lines") or [""])[0] if other["type"] == "paragraph" else None
            if text and heading_kind(text) == kind and heading_number(text) == number and in_toc(text, kind):
                return True
        return False

    # 부로 나뉜 문서에서 첫 부 앞의 번호 항목은 표지·요약의 목록이다.
    first_part = next((i for i, n in enumerate(nodes) if n["type"] == "heading" and n["kind"] == "part"), None)
    sections, stack = [], []
    last_number = {}  # (부모 섹션 id, 번호 종류) -> 마지막 번호
    for index, node in enumerate(nodes):
        if first_part is not None and index < first_part and node["type"] == "heading":
            node.update(type="paragraph", lines=[node["text"]], list_item=True)
            node.pop("kind")
        if node["type"] == "heading" and node["kind"] in toc_titles and displaced(index):
            node.update(type="paragraph", lines=[node["text"]], list_item=True)
            node.pop("kind")
        if node["type"] == "heading":
            rank = order[node["kind"]]
            probe = list(stack)
            while probe and order[probe[-1]["kind"]] >= rank:
                probe.pop()
            parent_id = probe[-1]["id"] if probe else None
            number = heading_number(node["text"])
            previous = last_number.get((parent_id, node["kind"]))
            # 앞 번호보다 1~2 큰 번호만 제목으로 인정한다 (번호 하나가 빠진 경우까지 허용)
            if number is not None and previous is not None and not previous < number <= previous + 2:
                node["type"] = "paragraph"
                node["lines"] = [node["text"]]
                node["list_item"] = True
                node.pop("kind")
                node["section"] = stack[-1]["id"] if stack else None
                continue
            if number is not None:
                last_number[(parent_id, node["kind"])] = number
            stack = probe
            section = {"id": f"s{len(sections) + 1}", "title": node["text"], "kind": node["kind"],
                       "level": len(stack) + 1, "page": node["page"],
                       "parent": stack[-1]["id"] if stack else None,
                       "path": [s["title"] for s in stack] + [node["text"]]}
            sections.append(section)
            stack.append(section)
            node["section"] = section["id"]
        else:
            node["section"] = stack[-1]["id"] if stack else None
    return sections


def link_continuations(nodes, page_heights=None, *, ocr_margins=False):
    """쪽 경계의 표를 연결한다. 격자가 다르면 외곽·본문 여백을 확인하고 칸의 실제 x 범위로 머리글을 잇는다."""
    def margin_key(node):
        height = (page_heights or {}).get(node["page"])
        if not height or node["type"] not in ("paragraph", "heading"):
            return None
        # 본문 경계보다 바깥쪽에서 반복된 문구만 제외한다. 본문 제목·문단은 연결을 막는다.
        zone = ("top" if node["bbox"][3] < height * MARGIN_RATIO / 2 else
                "bottom" if node["bbox"][1] > height * (1 - MARGIN_RATIO / 2) else None)
        if not zone:
            return None
        key = squash(re.sub(r"\d+", "#", node["text"]))
        if ocr_margins:
            # Keep PDF matching exact; OCR varies in case, separators and I/l.
            key = "".join(c for c in key.casefold() if c.isalnum() or c == "#")
            key = key.translate(str.maketrans({"l": "i"}))
        return (zone, key)

    margins = defaultdict(set)
    for node in nodes:
        if (key := margin_key(node)) is not None:
            margins[key].add(node["page"])
    repeated = {key for key, pages in margins.items() if len(pages) >= 3}
    if ocr_margins:
        for key, pages in margins.items():
            if key in repeated or len(key[1]) < 8:
                continue
            # Count distinct pages of directly similar texts in the same zone.
            # Avoid transitive clustering that could absorb unrelated body text.
            similar_pages = set(pages)
            for other, other_pages in margins.items():
                if (other[0] == key[0] and len(other[1]) >= 8
                        and SequenceMatcher(None, key[1], other[1], autojunk=False).ratio() >= 0.8):
                    similar_pages.update(other_pages)
            if len(similar_pages) >= 3:
                repeated.add(key)
    by_page = defaultdict(list)
    for node in nodes:
        if node["type"] in ("table", "paragraph", "heading") and node.get("parent") is None:
            by_page[node["page"]].append(node)
    for page in sorted(by_page):
        previous = [n for n in by_page.get(page - 1, []) if margin_key(n) not in repeated]
        current = [n for n in by_page[page] if margin_key(n) not in repeated]
        if not previous or not current or previous[-1]["type"] != "table" or current[0]["type"] != "table":
            continue
        a, b = previous[-1], current[0]
        same_grid = len(a["col_edges"]) == len(b["col_edges"]) and all(
            abs(x - y) <= CONTINUE_TOLERANCE for x, y in zip(a["col_edges"], b["col_edges"])
        )
        # 격자가 달라도 본문 아래·위 끝을 채우고 좌우 외곽이 같으면 이어진 표다.
        at_boundary = (page_heights and page - 1 in page_heights and page in page_heights
                       and a["bbox"][3] >= page_heights[page - 1] * (1 - MARGIN_RATIO)
                       and b["bbox"][1] <= page_heights[page] * MARGIN_RATIO)
        same_frame = all(abs(a["bbox"][i] - b["bbox"][i]) <= CONTINUE_TOLERANCE for i in (0, 2))
        if same_grid or (at_boundary and same_frame):
            b["continued_from"] = a["id"]
            if not same_grid or a is not by_page[page - 1][-1] or b is not by_page[page][0]:
                # 반복된 열 머리글이 있으면 다음 표가 스스로 만든 경로를 사용한다.
                headers = {squash(c["text"]) for c in a["cells"] if c["r"] == 0 and c["header"]}
                if any(c["r"] == 0 and c["header"] and squash(c["text"]) in headers for c in b["cells"]):
                    continue
                for cell in b["cells"]:
                    if cell["header"] or cell.get("col_path") or not cell["text"]:
                        continue
                    # 마지막 행의 병합 칸 경계를 비교한다. 이전 행에서만 합쳐진 칸의 머리글은 주지 않는다.
                    paths = {tuple(c["col_path"]) for c in a["cells"]
                             if not c["header"] and c.get("col_path")
                             and c["r"] + c["rs"] == a["n_rows"]
                             and all(abs(c["bbox"][i] - cell["bbox"][i]) <= CONTINUE_TOLERANCE for i in (0, 2))}
                    if len(paths) == 1:
                        cell["col_path"] = list(paths.pop())
                        cell["col_path_from"] = a["id"]
                continue
            columns = {}
            for cell in a["cells"]:
                if not cell["header"] and cell.get("col_path"):
                    for c in range(cell["c"], cell["c"] + cell["cs"]):
                        columns.setdefault(c, cell["col_path"])
            for cell in b["cells"]:
                if not cell["header"] and not cell.get("col_path") and cell["c"] in columns:
                    cell["col_path"] = columns[cell["c"]]
                    cell["col_path_from"] = a["id"]


# ---------------------------------------------------------------- 문서 정보


def document_info(nodes, tables):
    texts = []
    for node in nodes:
        if node["type"] in ("paragraph", "heading"):
            texts.append((node["page"], node["text"]))
    for table in tables:
        texts.extend((table["page"], cell["text"]) for cell in table["cells"] if cell["text"])
    info = {}

    def first(name, pattern, pages=8):
        for page, text in texts:
            if page <= pages and (match := re.search(pattern, text)):
                info[name] = {"value": re.sub(r"\s+", "", match.group(1)), "page": page}
                return

    first("fund_code", r"펀드\s*코드\s*[:：]?\s*([A-Z0-9]{4,})")
    date_pattern = r"(\d{4}\s*[.\-/년]\s*\d{1,2}\s*[.\-/월]\s*\d{1,2}(?:\s*일)?)"
    first("as_of_date", r"작성\s*기준일\s*[:：]?\s*" + date_pattern)
    if "as_of_date" not in info:
        # An unqualified summary date is a document date; compound labels such
        # as 심사기준일 and 분배금 지급기준일 must never match this fallback.
        first("as_of_date", r"(?:<\s*요약정보\s*>\s*\(?|^\s*[\[(]?)기준일\s*[:：]\s*" + date_pattern)
    first("doc_type", r"((?:간이)?투자설명서)", pages=2)
    names = Counter()
    for page, text in texts:
        for match in re.finditer(r"([A-Za-z가-힣0-9\-·&]+(?:\s+[A-Za-z가-힣0-9\-·&]+){0,6}?\s*(?:투자신탁|투자회사)\s*\[[^\]]{1,20}\])", text):
            names[re.sub(r"\s+", " ", match.group(1)).strip()] += 1
    if names:
        name, count = names.most_common(1)[0]
        info["fund_name"] = {"value": name, "occurrences": count}
    return info


# ---------------------------------------------------------------- 실행


def build(pdf_path, layer1, ocr):
    repeated = repeated_keys(layer1)
    ocr_pages = {p["page"]: p["lines"] for p in ocr["pages"]} if ocr else {}
    layer1_pages = {p["page"]: p for p in layer1["pages"]}
    counter = Counter()
    nodes, tables, coverage = [], [], {}
    with pdfplumber.open(pdf_path) as pdf:
        # 문서 전체의 본문 오른쪽 끝: 쪽마다 가장 오른쪽 글자 위치의 중앙값
        rights = sorted(max(c["x1"] for c in page.chars) for page in pdf.pages if page.chars)
        text_right = rights[len(rights) // 2] if rights else 0
        previous_toc = False
        for page in pdf.pages:
            fix_upright(page)
            number = page.page_number
            ocr_lines = ocr_pages.get(number)
            full_lines = [line for line in lines_in(page, (0, 0, page.width, page.height))
                          if not is_repeated(line, page.height, repeated)]
            fills = fills_of(page)
            page_tables = ruled_tables(page, number, fills, ocr_lines, counter)
            link_nested(page_tables)
            # 표 영역 전체가 아니라 칸만 뺀다. 칸으로 잡히지 않은 글자가 버려지지 않고 문단으로 남는다.
            cell_boxes = [c["bbox"] for t in page_tables if t["parent"] is None for c in t["cells"]]

            lines = [line for line in lines_in(page, (0, 0, page.width, page.height), exclude=cell_boxes)
                     if not is_repeated(line, page.height, repeated)]
            free_tables, used = aligned_tables(page, number, lines, counter)
            lines = [line for i, line in enumerate(lines) if i not in used]
            toc_page = is_toc_page(lines, number, len(pdf.pages), previous_toc)
            # 목차 전체가 표로 잡혔으면 표를 풀어 제목 줄을 보존한다.
            full_headings = sum(heading_kind(line["text"]) is not None for line in full_lines)
            free_headings = sum(heading_kind(line["text"]) is not None for line in lines)
            if (is_toc_page(full_lines, number, len(pdf.pages), previous_toc)
                    and (not toc_page or full_headings >= TOC_PAGE_MIN_LINES and free_headings < full_headings / 2)):
                toc_page, lines, page_tables, free_tables = True, full_lines, [], []
            previous_toc = toc_page
            page_nodes = paragraphs(lines, page.width, counter, number, text_right, toc_page)

            for block in layer1_pages[number]["blocks"]:
                if block["type"] != "image":
                    continue
                counter["node"] += 1
                image = {"id": f"n{counter['node']}", "type": "image", "page": number, "bbox": block["bbox"],
                         "file": block.get("file"), "likely_has_text": (block.get("text_check") or {}).get("likely_has_text")}
                if ocr_lines is not None:
                    found = ocr_in(ocr_lines, block["bbox"])
                    image["ocr_lines"] = [{"text": l["text"], "score": l["score"], "bbox": l["bbox"]} for l in found]
                page_nodes.append(image)

            page_tables = page_tables + free_tables
            page_nodes.extend(page_tables)
            page_nodes.sort(key=lambda n: (n["bbox"][1], n["bbox"][0]))
            nodes.extend(page_nodes)
            tables.extend(page_tables)

            # 빠진 글자 확인: 반복 문구를 뺀 PDF 글자가 구조 결과에 모두 있는지
            source = Counter(c["text"] for c in page.chars if c["text"].strip())
            removed = Counter("".join(l["text"] for l in lines_in(page, (0, 0, page.width, page.height))
                                      if is_repeated(l, page.height, repeated)))
            got = Counter(squash("".join(
                [n.get("raw", n.get("text", "")) for n in page_nodes if n["type"] != "table"]
                + [c["text"] for t in page_tables for c in t["cells"] if c.get("source") == "pdf_text"]
                + [note["text"] for t in page_tables for note in t["notes"]]
                + [t.get(k, "") for t in page_tables for k in ("unit", "caption")]
            )))
            lost = sum((source - removed - got).values())
            if lost:
                coverage[str(number)] = lost

    nodes, sections = finalize(nodes, tables, counter, {p["page"]: p["height"] for p in layer1["pages"]})
    return {
        "structure_version": STRUCTURE_VERSION,
        "source": layer1["source"],
        "document": document_info(nodes, tables),
        "sections": sections,
        "nodes": nodes,
        "check": {"missing_chars_by_page": coverage, "tables": len(tables),
                  "ocr_pages": sorted(ocr_pages)},
    }


def finalize(nodes, tables, counter, page_heights=None, *, ocr_margins=False):
    """쪽별 노드를 문서 단위로 마무리한다: 캡션·주석 붙이기, 목차 제목 복구, 섹션, 이어진 표, 격자와 값."""
    nodes = attach_captions(nodes)
    nodes = recover_toc_headings(nodes, counter)
    # attach_captions가 표 노드 뒤 주석을 옮겼으므로 표 밖 주석 글자는 그대로 보존된다
    sections = build_sections(nodes)
    link_continuations(nodes, page_heights, ocr_margins=ocr_margins)
    for table in tables:
        # 행×열 글자 행렬. 병합 칸은 덮는 모든 자리에 같은 글자를 넣는다.
        grid = [["" for _ in range(table["n_cols"])] for _ in range(table["n_rows"])]
        for cell in table["cells"]:
            for r in range(cell["r"], min(cell["r"] + cell["rs"], table["n_rows"])):
                for c in range(cell["c"], min(cell["c"] + cell["cs"], table["n_cols"])):
                    grid[r][c] = cell["text"]
        table["grid"] = grid
        table["values"] = [
            {"r": c["r"], "c": c["c"], "text": c["text"], "col_path": c.get("col_path", []),
             "row_path": c.get("row_path", []), "context_path": table["context_path"], "source": c["source"]}
            for c in table["cells"] if not c["header"] and c["text"]
        ]
    return nodes, sections


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("layer1", type=Path)
    parser.add_argument("--ocr", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    layer1 = json.loads(args.layer1.read_text(encoding="utf-8"))
    ocr = json.loads(args.ocr.read_text(encoding="utf-8")) if args.ocr else None
    result = build(args.pdf, layer1, ocr)
    output = args.output or args.layer1.with_name(args.layer1.stem + ".structure.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    kinds = Counter(n["type"] for n in result["nodes"])
    print(f"output={output} nodes={dict(kinds)} sections={len(result['sections'])} "
          f"check={result['check']['missing_chars_by_page'] or 'ok'} document={result['document']}")


if __name__ == "__main__":
    main()
