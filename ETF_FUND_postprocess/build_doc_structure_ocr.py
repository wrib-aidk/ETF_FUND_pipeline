"""2층 OCR 전용 모드: PDF의 글자·선을 쓰지 않고 쪽 이미지와 OCR 줄만으로 문서 구조를 만든다.

스캔본처럼 PDF에 글자가 없는 경우를 대비한 경로이고, PDF 기반 결과(build_doc_structure.py)와 비교해 검증한다.
- 표 선: 쪽 이미지에서 가로·세로 경계 픽셀을 찾는다. 배경색 칸의 경계도 선으로 본다.
- 칸 배경색: 칸 안쪽 픽셀의 가장 흔한 색.
- 칸 글자, 문단: OCR 줄. 같은 높이의 OCR 조각은 한 줄로 합친다.
- 반복 머리말·꼬리말: 여러 쪽의 위·아래 여백에 같은 OCR 글자가 나오면 뺀다.
- 머리글·경로·문단·섹션·목차 규칙은 build_doc_structure.py의 함수를 그대로 쓴다.

1층 JSON에서는 쪽 번호·크기·이미지 파일 경로만 쓴다 (표 영역 등 PDF에서 나온 정보는 쓰지 않는다).

사용법:
    python build_doc_structure_ocr.py 추출.json OCR.json [-o 출력.json]
"""
import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from pdfplumber.table import cells_to_tables, edges_to_intersections, intersections_to_cells, join_edge_group

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_doc_structure as base  # noqa: E402

COLOR_STEP = 40  # 바로 위아래(좌우) 픽셀의 색 차이가 이보다 크면 경계
FILL_STEP = 8  # 옅은 면 경계의 색 차이. 일반 경계보다 긴 구간만 허용한다
LINE_INTERIOR = 1.5  # pt. 얇은 선 판정에서 비교할 양옆 거리
LINE_CONTRAST = 25  # 위아래(좌우)보다 이만큼 어두우면 배경색 위의 선
MIN_H_RUN = 12  # pt. 이보다 짧은 가로 픽셀 줄은 글자 획으로 본다
MIN_V_RUN = 5  # pt. 세로 획
MIN_FILL_H_RUN = 24  # pt. 옅은 가로 경계의 최소 길이
MIN_FILL_V_RUN = 10  # pt. 옅은 세로 경계의 최소 길이
SNAP = 1  # pt. 이만큼 가까운 선은 같은 선 (선 굵기 수준. 크면 붙어 있는 두 표의 테두리가 합쳐진다)
CROSS = 1.5  # pt. 선이 이만큼 안에서 만나면 교차
JOIN = 3  # pt. 같은 선 위에서 이만큼 끊긴 것은 잇는다
MIN_EDGE = 4  # pt. 합친 뒤에도 이보다 짧은 선은 버린다
PARALLEL = 3  # pt. 굵은 선의 양쪽 경계나 이중선 사이 최대 거리 (경계 픽셀 포함)
ENDPOINT = 2  # pt. 이중선은 양 끝점까지 같아야 한다 (안쪽 표 테두리 보호)
ROW_OVERLAP = 0.5  # OCR 조각끼리 높이가 이 비율 이상 겹치면 같은 줄
REPEAT_PAGE_RATIO = 0.3  # 이 비율 이상의 쪽 여백에 나오면 반복 문구


def clean_ocr_text(text):
    """OCR 글자 정리: 숫자 사이 쉼표·점 옆의 공백("8, 864" → "8,864"), 숫자 앞뒤의 긴 대시(– —)를 '-'로,
    글자 옆 세로 표 선을 읽은 '|'는 뺀다 (투자설명서 본문에는 세로 막대 문자가 쓰이지 않는다)."""
    text = re.sub(r"\s*[|｜]\s*", " ", text)
    text = re.sub(r"(?<=\d)\s*([,.])\s+(?=\d)|(?<=\d)\s+([,.])(?=\d)", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"[–—−](?=\s*\d)|(?<=\d)\s*[–—−]|^[–—−]$", "-", text)
    return text.strip()


def runs(mask_1d, min_len):
    """1차원 참/거짓 배열에서 길이 min_len 이상인 참 구간 (시작, 끝)."""
    padded = np.concatenate(([False], mask_1d, [False]))
    change = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = change[0::2], change[1::2]
    keep = ends - starts >= min_len
    return zip(starts[keep], ends[keep])


def inside_text(box, edge, orientation):
    """글자 상자 안쪽에 완전히 들어가는 짧은 획인지 (표 선은 글자 상자 가장자리나 바깥에 있다)."""
    x0, y0, x1, y1 = box
    if orientation == "h":
        h = y1 - y0
        return x0 <= edge["x0"] and edge["x1"] <= x1 and y0 + h * 0.25 <= edge["top"] <= y1 - h * 0.25
    # OCR 조각이 칸 경계를 넘어 읽히는 경우가 있으므로, 글자 높이보다 긴 세로선은 표 선으로 남긴다
    return (x0 + 1 <= edge["x0"] <= x1 - 1 and y0 - 1 <= edge["top"] and edge["bottom"] <= y1 + 1
            and edge["bottom"] - edge["top"] <= (y1 - y0) * 0.9)


def detect_edges(pixels, scale, text_boxes):
    """쪽 이미지에서 표 선 후보를 찾는다.

    색이 크게 바뀌는 경계(흰 바탕↔면, 면↔다른 색 면, 선의 양 가장자리)와, 양쪽보다 확실히 어두운 얇은 줄
    (배경색 위에 그은 선)을 선 픽셀로 본다. 옅은 면 경계는 긴 구간만 남기는 방향별 열기로 추출한다.
    OCR 상자에 가려진 틈을 잇고, 양 끝점까지 같은 굵은 선·이중선의 경계를 중심선으로 줄인다.
    """
    color = pixels.astype(np.int16)
    step_h = np.zeros(pixels.shape[:2], dtype=np.int16)
    step_v = np.zeros(pixels.shape[:2], dtype=np.int16)
    step_h[1:-1] = np.abs(color[2:] - color[:-2]).max(axis=2)
    step_v[:, 1:-1] = np.abs(color[:, 2:] - color[:, :-2]).max(axis=2)
    k = max(2, round(LINE_INTERIOR * scale))
    light = pixels.min(axis=2).astype(np.int16)
    lu, ld, ll, lr = np.full_like(light, 255), np.full_like(light, 255), np.full_like(light, 255), np.full_like(light, 255)
    lu[k:], ld[:-k] = light[:-k], light[k:]
    ll[:, k:], lr[:, :-k] = light[:, :-k], light[:, k:]
    horizontal = (step_h > COLOR_STEP) | (light < np.minimum(lu, ld) - LINE_CONTRAST)
    vertical = (step_v > COLOR_STEP) | (light < np.minimum(ll, lr) - LINE_CONTRAST)

    # 옅은 면 경계는 긴 구간만 남겨 글자 가장자리의 안티앨리어싱과 구별한다.
    faint_h, faint_v = step_h > FILL_STEP, step_v > FILL_STEP
    for y in range(horizontal.shape[0]):
        for x0, x1 in runs(faint_h[y], MIN_FILL_H_RUN * scale):
            horizontal[y, x0:x1] = True
    for x in range(vertical.shape[1]):
        for y0, y1 in runs(faint_v[:, x], MIN_FILL_V_RUN * scale):
            vertical[y0:y1, x] = True

    edges = []
    for y in range(horizontal.shape[0]):
        for x0, x1 in runs(horizontal[y], MIN_H_RUN * scale):
            edges.append({"x0": x0 / scale, "x1": x1 / scale, "top": y / scale, "bottom": y / scale,
                          "width": (x1 - x0) / scale, "height": 0, "orientation": "h"})
    for x in range(vertical.shape[1]):
        for y0, y1 in runs(vertical[:, x], MIN_V_RUN * scale):
            edges.append({"x0": x / scale, "x1": x / scale, "top": y0 / scale, "bottom": y1 / scale,
                          "width": 0, "height": (y1 - y0) / scale, "orientation": "v"})
    edges = merge_parallel(bridge_text(snap_window(edges), text_boxes))
    edges = [e for e in edges if (e["x1"] - e["x0"] if e["orientation"] == "h" else e["bottom"] - e["top"]) >= MIN_EDGE]
    edges = [e for e in edges if not any(inside_text(b, e, e["orientation"]) for b in text_boxes)]
    return close_open_edges(edges, pixels.shape[1] / scale, pixels.shape[0] / scale)


def close_open_edges(edges, width, height):
    """같은 격자의 긴 세로선 끝이 나란히 열린 경우 위·아래 테두리를 보완한다.

    두 가로선과 모두 교차하는 세로선만 묶어 글자 획과 별개 표를 잇지 않는다.
    일부 칸에만 남은 테두리는 같은 높이에서 늘리고, 쪽 여백에는 선을 만들지 않는다.
    """
    horizontal = [e for e in edges if e["orientation"] == "h"]
    # 짧은 표까지 닫으면 독립된 작은 격자의 끝을 새 행으로 오인하기 쉬워 긴 격자만 보완한다.
    vertical = [e for e in edges if e["orientation"] == "v" and e["height"] >= height * 0.3]

    def crosses(h, v):
        return (h["x0"] - CROSS <= v["x0"] <= h["x1"] + CROSS
                and v["top"] - CROSS <= h["top"] <= v["bottom"] + CROSS)

    added = []
    for end in ("top", "bottom"):
        groups = []
        for v in sorted(vertical, key=lambda e: e[end]):
            if groups and v[end] - groups[-1][0][end] <= ENDPOINT:
                groups[-1].append(v)
            else:
                groups.append([v])
        for group in groups:
            seen = set()
            for h in horizontal:
                vs = [v for v in group if crosses(h, v)]
                xs = sorted({v["x0"] for v in vs})
                if len(xs) < 2 or xs[-1] - xs[0] < MIN_FILL_H_RUN or tuple(xs) in seen:
                    continue
                seen.add(tuple(xs))
                support = [line["top"] for line in horizontal if all(crosses(line, v) for v in vs)]
                if max(support) - min(support) < MIN_H_RUN:
                    continue
                y = (min(v[end] for v in vs) + max(v[end] for v in vs)) / 2
                if not (MIN_H_RUN <= xs[0] < xs[-1] <= width - MIN_H_RUN
                        and MIN_H_RUN <= y <= height - MIN_H_RUN):
                    continue
                if all(any(abs(line["top"] - y) <= CROSS and crosses(line, v)
                           for line in horizontal + added) for v in vs):
                    continue
                nearby = [line["top"] for line in horizontal if abs(line["top"] - y) <= CROSS
                          and line["x0"] <= xs[-1] and line["x1"] >= xs[0]]
                if nearby:
                    y = min(nearby, key=lambda value: abs(value - y))
                elif min(abs(value - y) for value in support) < MIN_H_RUN:
                    # 닫힌 표 밖으로 조금 삐져나온 세로선은 새 행으로 만들지 않는다.
                    continue
                added.append({"x0": xs[0], "x1": xs[-1], "top": y, "bottom": y,
                              "width": xs[-1] - xs[0], "height": 0, "orientation": "h"})
    return edges + added


def bridge_text(edges, text_boxes):
    """같은 직선의 틈 전체가 OCR 상자에 가려진 경우에만 양쪽 선을 잇는다."""
    groups = defaultdict(list)
    for edge in edges:
        groups[(edge["orientation"], edge["top"] if edge["orientation"] == "h" else edge["x0"])].append(edge)
    result = []
    for (orientation, position), group in groups.items():
        start, end = ("x0", "x1") if orientation == "h" else ("top", "bottom")
        group.sort(key=lambda e: e[start])
        current = dict(group[0])
        for edge in group[1:]:
            gap0, gap1 = current[end], edge[start]
            covered = any((b[1] - CROSS <= position <= b[3] + CROSS and b[0] - CROSS <= gap0 <= gap1 <= b[2] + CROSS)
                          if orientation == "h" else
                          (b[0] - CROSS <= position <= b[2] + CROSS and b[1] - CROSS <= gap0 <= gap1 <= b[3] + CROSS)
                          for b in text_boxes)
            if covered:
                current[end] = edge[end]
                current["width" if orientation == "h" else "height"] = current[end] - current[start]
            else:
                result.append(current)
                current = dict(edge)
        result.append(current)
    return result


def merge_parallel(edges):
    """거리와 양 끝점이 모두 일치하는 평행 경계만 중심선 하나로 줄인다. 사슬 병합은 하지 않는다."""
    result = []
    for orientation, key in (("h", "top"), ("v", "x0")):
        start, end = ("x0", "x1") if orientation == "h" else ("top", "bottom")
        items = sorted((e for e in edges if e["orientation"] == orientation), key=lambda e: e[key])
        used = set()
        for i, edge in enumerate(items):
            if i in used:
                continue
            group = [edge]
            for j in range(i + 1, len(items)):
                other = items[j]
                if other[key] - edge[key] > PARALLEL:
                    break
                if (j not in used and max(abs(other[start] - edge[start]), abs(other[end] - edge[end])) <= ENDPOINT
                        and min(other[end] - other[start], edge[end] - edge[start]) >= MIN_H_RUN):
                    group.append(other)
                    used.add(j)
            merged = dict(edge)
            position = (group[0][key] + group[-1][key]) / 2
            if orientation == "h":
                merged["top"] = merged["bottom"] = position
            else:
                merged["x0"] = merged["x1"] = position
            merged[start], merged[end] = min(e[start] for e in group), max(e[end] for e in group)
            merged["width" if orientation == "h" else "height"] = merged[end] - merged[start]
            result.append(merged)
    return result


def snap_window(edges):
    """같은 방향 선을 위치로 묶어 하나로 맞추고, 같은 줄 위에서 끊긴 것을 잇는다.

    묶음은 첫 선에서 SNAP 안에 드는 것까지만이다. pdfplumber의 snap은 가까운 것끼리 사슬로 이어 묶어서,
    1pt 간격으로 늘어선 선(선의 양쪽 가장자리, 바로 옆 다른 표의 테두리)이 모두 하나로 합쳐진다.
    """
    result = []
    for orientation, key in (("h", "top"), ("v", "x0")):
        items = sorted((e for e in edges if e["orientation"] == orientation), key=lambda e: e[key])
        groups = []
        for edge in items:
            if groups and edge[key] - groups[-1][0][key] <= SNAP:
                groups[-1].append(edge)
            else:
                groups.append([edge])
        for group in groups:
            position = sorted(e[key] for e in group)[len(group) // 2]
            moved = []
            for e in group:
                e = dict(e)
                if orientation == "h":
                    e["top"] = e["bottom"] = position
                else:
                    e["x0"] = e["x1"] = position
                moved.append(e)
            result.extend(join_edge_group(moved, orientation, JOIN))
    return result


def cell_fill(pixels, scale, box):
    """칸 안쪽에서 가장 흔한 색. 흰색이면 None."""
    pad = 1.5
    x0, y0 = int((box[0] + pad) * scale), int((box[1] + pad) * scale)
    x1, y1 = int((box[2] - pad) * scale), int((box[3] - pad) * scale)
    if x1 <= x0 or y1 <= y0:
        return None
    region = pixels[y0:y1:2, x0:x1:2].reshape(-1, 3)
    if not len(region):
        return None
    quantized = (region // 8).astype(np.int32)
    keys, counts = np.unique(quantized[:, 0] * 1024 + quantized[:, 1] * 32 + quantized[:, 2], return_counts=True)
    key = int(keys[counts.argmax()])
    rgb = [((key // 1024) * 8 + 4) / 255, ((key // 32 % 32) * 8 + 4) / 255, ((key % 32) * 8 + 4) / 255]
    if min(rgb) >= 0.98:
        return None
    return tuple(round(v * 20) / 20 for v in rgb)  # 0.05 단위로 맞춰 같은 색끼리 같게 본다


def rows_of(pieces):
    """OCR 조각을 줄로 합친다. 높이가 많이 겹치는 조각은 같은 줄이다. 줄마다 조각(members)을 남긴다."""
    rows = []
    for piece in sorted(pieces, key=lambda p: (base.center(p["bbox"])[1], p["bbox"][0])):
        y0, y1 = piece["bbox"][1], piece["bbox"][3]
        for row in rows:
            r0, r1 = row["bbox"][1], row["bbox"][3]
            overlap = min(y1, r1) - max(y0, r0)
            if overlap > 0 and overlap >= ROW_OVERLAP * min(y1 - y0, r1 - r0):
                row["members"].append(piece)
                row["bbox"] = [min(row["bbox"][0], piece["bbox"][0]), min(r0, y0),
                               max(row["bbox"][2], piece["bbox"][2]), max(r1, y1)]
                break
        else:
            rows.append({"bbox": list(piece["bbox"]), "members": [piece]})
    for row in rows:
        row["members"].sort(key=lambda p: p["bbox"][0])
        row["text"] = " ".join(p["text"].strip() for p in row["members"]).strip()
        row["bbox"] = base.rounded(row["bbox"])
    rows.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]))
    return [r for r in rows if r["text"]]


def segments(row):
    """선 없는 표용 칸 후보: 줄 안의 OCR 조각을 큰 간격에서 나눈다."""
    height = row["bbox"][3] - row["bbox"][1]
    groups = [[row["members"][0]]]
    for piece in row["members"][1:]:
        if piece["bbox"][0] - groups[-1][-1]["bbox"][2] > height * base.ALIGNED_GAP_RATIO:
            groups.append([piece])
        else:
            groups[-1].append(piece)
    return [{"text": " ".join(p["text"] for p in g),
             "bbox": base.rounded((g[0]["bbox"][0], min(p["bbox"][1] for p in g), g[-1]["bbox"][2], max(p["bbox"][3] for p in g)))}
            for g in groups]


def pieces_in(pieces, box, exclude=()):
    return [p for p in pieces if base.inside(box, *base.center(p["bbox"]), pad=1)
            and not any(base.inside(other, *base.center(p["bbox"])) for other in exclude)]


def regrid_ocr(cells):
    """OCR 상자는 글자 여백까지 포함하므로 PDF보다 좁은 간격으로 행을 나눈다."""
    stacked = [c for c in cells if len(c["_lines"]) >= base.COLLAPSED_MIN_LINES
               and sum(base.is_numeric(line["text"]) for line in c["_lines"]) >= 2]
    if len({c["c"] for c in stacked}) < 2:
        return cells, None
    lines = [line for c in cells for line in c["_lines"]]
    height = sorted(line["bbox"][3] - line["bbox"][1] for line in lines)[len(lines) // 2]
    rows = base.cluster([base.center(line["bbox"])[1] for line in lines], tolerance=height * 0.4)
    result = []
    for cell in cells:
        if len(cell["_lines"]) >= 2:
            groups = defaultdict(list)
            for line in cell["_lines"]:
                groups[base.nearest(base.center(line["bbox"])[1], rows)].append(line)
            for row, group in groups.items():
                box = [min(line["bbox"][0] for line in group), min(line["bbox"][1] for line in group),
                       max(line["bbox"][2] for line in group), max(line["bbox"][3] for line in group)]
                texts = [line["text"] for line in group]
                result.append(dict(cell, r=row, rs=1, bbox=box, _lines=group, lines=texts, text=" ".join(texts)))
        elif cell["_lines"]:
            covered = [i for i, y in enumerate(rows) if cell["bbox"][1] <= y <= cell["bbox"][3]]
            covered = covered or [base.nearest(base.center(cell["_lines"][0]["bbox"])[1], rows)]
            result.append(dict(cell, r=covered[0], rs=covered[-1] - covered[0] + 1))
        else:
            result.append(dict(cell, r=base.nearest(cell["bbox"][1], rows), rs=1))
    return base.without_overlaps(result), len(rows)


def merge_split_row_labels(cells, n_rows):
    """Join label-only OCR rows to a neighbouring body row, never column headers."""
    base.mark_row_labels(cells)
    at = defaultdict(list)
    for cell in cells:
        if cell["text"]:
            for r in range(cell["r"], cell["r"] + cell["rs"]):
                at[r].append(cell)
    numeric = lambda row: [c for c in row if c["c"] > 0 and base.is_numeric(c["text"])
                           and not c["header"]]
    body_rows = [r for r, row in at.items() if len(numeric(row)) >= 2]
    if not body_rows:
        return cells, n_rows
    first_body = min(body_rows)
    label_cols = {c["c"] for r in body_rows for c in at[r]
                  if c["header"] and c["cs"] == 1 and not base.is_numeric(c["text"])
                  and c["c"] < min(v["c"] for v in numeric(at[r]))}
    removed, replacements = set(), {}
    dots = re.compile(r"^[\s.…·ㆍ⋯・]+$")
    for r in sorted(body_rows):
        for col in sorted(label_cols):
            if any(c["c"] <= col < c["c"] + c["cs"] for c in at[r]):
                continue
            # A value row must have values to the right of this exact label column.
            if len([c for c in numeric(at[r]) if c["c"] > col]) < 2:
                continue
            fragments = []
            value_top = min(c["bbox"][1] for c in at[r])
            value_bottom = max(c["bbox"][3] for c in at[r])
            height = value_bottom - value_top
            for direction in (-1, 1):
                edge = value_top if direction < 0 else value_bottom
                step = r + direction
                while first_body <= step < n_rows and step not in removed:
                    row = at[step]
                    if len(row) != 1:
                        break
                    cell = row[0]
                    if (cell["c"] != col or cell["cs"] != 1 or cell["rs"] != 1
                            or len(cell["text"]) > base.ROW_LABEL_MAX_CHARS
                            or re.search(r"\d", cell["text"])
                            or not (dots.fullmatch(cell["text"]) or re.search(r"[^\W\d_]", cell["text"]))):
                        break
                    gap = edge - cell["bbox"][3] if direction < 0 else cell["bbox"][1] - edge
                    if gap > height * 0.8:
                        break
                    fragments.append(cell)
                    edge = cell["bbox"][1] if direction < 0 else cell["bbox"][3]
                    step += direction
            if not fragments or not any(not dots.fullmatch(c["text"]) for c in fragments):
                continue
            fragments.sort(key=lambda c: c["r"])
            texts = [c["text"] for c in fragments if not dots.fullmatch(c["text"])]
            label = dict(fragments[0], r=r, rs=1, text=" ".join(texts), lines=texts,
                         ocr_text=" ".join(c.get("ocr_text", c["text"]) for c in fragments),
                         _lines=[line for c in fragments for line in c.get("_lines", [])], header=True)
            label["bbox"] = [min(c["bbox"][0] for c in fragments), min(value_top, *(c["bbox"][1] for c in fragments)),
                             max(c["bbox"][2] for c in fragments), max(value_bottom, *(c["bbox"][3] for c in fragments))]
            replacements[(r, col)] = label
            removed.update(c["r"] for c in fragments)
    if not removed:
        return cells, n_rows
    result = []
    for cell in cells:
        if cell["r"] in removed or (cell["r"], cell["c"]) in replacements:
            continue
        covered = [r for r in range(cell["r"], cell["r"] + cell["rs"]) if r not in removed]
        if covered:
            result.append(dict(cell, r=covered[0] - sum(r < covered[0] for r in removed), rs=len(covered)))
    result.extend(dict(c, r=r - sum(old < r for old in removed)) for (r, _), c in replacements.items())
    return sorted(result, key=lambda c: (c["r"], c["c"])), n_rows - len(removed)


# OCR-only unit rules; leave the shared PDF caption behaviour unchanged.
UNIT_TOKEN = r"(?:[십백천만억조]+\s*)?(?:원|좌|주|명|개|배|년|월|일)|[십백천만억조]+|[%％]"
UNIT_LIST = rf"(?:{UNIT_TOKEN})(?:\s*[,/·]\s*(?:{UNIT_TOKEN}))*"
UNIT_FRAGMENT = re.compile(rf"[\[(<]?\s*단\s*위\s*[:：]\s*(?P<unit>{UNIT_LIST})\s*[\])>]?")
BARE_MONEY_UNIT = re.compile(r"[\[(]\s*(?P<unit>(?:[십백천만억조]+\s*)?원)\s*[\])]")


def unit_fragment(text):
    match = UNIT_FRAGMENT.search(text) or BARE_MONEY_UNIT.search(text)
    if match:
        return base.squash(match["unit"]).replace("％", "%"), match.group().strip()
    return None


def detect_table_units(tables, page_rows):
    """Read only the adjacent caption area, then top cells or a parent caption cell."""
    by_id = {t["id"]: t for t in tables}
    for table in tables:
        top = table["bbox"][1]
        floor = max([top - 40] + [t["bbox"][3] for t in tables if t is not table
                    and t["bbox"][3] <= top and base.overlap_area(
                        [table["bbox"][0], top - 40, table["bbox"][2], top], t["bbox"]) > 0])
        above = [row for row in page_rows if floor <= row["bbox"][1] and row["bbox"][3] <= top + 1]
        found = None
        for row in sorted(above, key=lambda row: row["bbox"][1], reverse=True):
            found = unit_fragment(row["text"])
            if found:
                break
            text = row["text"]
            if (len(text) > 60 or base.SENTENCE_END.search(text)
                    or any(pattern.match(text) for _, pattern in base.HEADING_PATTERNS)):
                break
        if not found:
            for cell in sorted(table["cells"], key=lambda c: (c["r"], c["c"])):
                if cell["r"] <= 2 and (found := unit_fragment(cell["text"])):
                    break
        if not found and table.get("parent"):
            parent = by_id[table["parent"]["table"]]
            holder = next(c for c in parent["cells"] if table["id"] in c.get("child_tables", []))
            found = unit_fragment(holder["text"])
        if found:
            table["unit"], table["unit_raw"] = found


def inherit_table_units(nodes, tables):
    """Inherit explicit page continuations or immediately adjacent same-grid splits."""
    by_id = {t["id"]: t for t in tables}
    for table in sorted(tables, key=lambda t: (t["page"], t["bbox"][1], t["bbox"][0])):
        # Shared finalization can attach an old-style raw unit paragraph.
        if table.get("unit"):
            found = unit_fragment(table["unit"])
            if found:
                table["unit"], table["unit_raw"] = found
            continue
        prior = by_id.get(table.get("continued_from"))
        if prior is None:
            adjacent = [t for t in tables if t is not table and t["page"] == table["page"]
                        and t.get("parent") == table.get("parent") and t.get("section") == table.get("section")
                        and 0 <= table["bbox"][1] - t["bbox"][3] <= 20
                        and len(t["col_edges"]) == len(table["col_edges"])
                        and all(abs(a - b) <= base.CONTINUE_TOLERANCE
                                for a, b in zip(t["col_edges"], table["col_edges"]))]
            prior = max(adjacent, key=lambda t: t["bbox"][3], default=None)
            if prior and any(n["page"] == table["page"] and n["type"] != "table"
                             and prior["bbox"][3] <= n["bbox"][1] < table["bbox"][1] for n in nodes):
                prior = None
        if prior and prior.get("unit"):
            table.update(unit=prior["unit"], unit_raw=prior.get("unit_raw", prior["unit"]), unit_from=prior["id"])


def pixel_dash(pixels, scale, box):
    """테두리를 뺀 칸 안에 중앙의 짧고 얇은 가로획 하나만 있는지 본다."""
    pad = 2  # pt. 격자선과 선 가장자리의 안티앨리어싱을 제외한다.
    x0, y0 = np.ceil((np.array(box[:2]) + pad) * scale).astype(int)
    x1, y1 = np.floor((np.array(box[2:]) - pad) * scale).astype(int)
    region = pixels[y0:y1, x0:x1]
    if not region.size:
        return False
    light = region.min(axis=2)
    ink = light < min(140, float(np.median(light)) - 60)
    ys, xs = np.nonzero(ink)
    if not len(xs):
        return False
    w, h = (xs.max() - xs.min() + 1) / scale, (ys.max() - ys.min() + 1) / scale
    cx, cy = (x0 + (xs.min() + xs.max() + 1) / 2) / scale, (y0 + (ys.min() + ys.max() + 1) / 2) / scale
    if not (1.5 <= w <= 7 and h <= 1.5 and w >= 2.5 * h
            and abs(cx - base.center(box)[0]) <= max(3, (box[2] - box[0]) * 0.15)
            and abs(cy - base.center(box)[1]) <= max(3, (box[3] - box[1]) * 0.2)):
        return False
    stroke = ink[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    # 떨어진 점이나 여러 획은 대시로 채우지 않는다.
    return bool(stroke.mean() >= 0.65 and stroke.any(axis=0).all()
                and all(len(list(runs(row, 1))) == 1 for row in stroke))


def correct_single_cells(cells, pixels, scale):
    """머리글 판정 뒤, 실제 숫자가 과반인 행·열의 혼동 글자와 OCR 없는 대시만 보완한다."""
    body = [c for c in cells if c["text"] and not c["header"] and not c.get("child_tables")]

    def numeric_context(cell):
        for axis, span in (("r", "rs"), ("c", "cs")):
            peers = [c for c in body if c is not cell and c[axis] < cell[axis] + cell[span]
                     and cell[axis] < c[axis] + c[span]]
            count = sum(base.is_numeric(c["text"]) for c in peers)
            if count >= 2 and count * 2 > len(peers):
                return True
        return False

    # 문맥은 보정 전 글자로만 계산하여 연쇄 치환하지 않는다. ∞와 화살표는 의미가 있을 수 있어 남긴다.
    zeros = [c for c in body if c["text"] in {"O", "o", "이", "○"} and numeric_context(c)]
    for cell in cells:
        if cell["header"] or cell.get("child_tables"):
            continue
        if any(cell is c for c in zeros):
            cell["ocr_text"] = cell["text"]
            cell["text"], cell["lines"] = "0", ["0"]
        elif (not cell["text"] and not cell["_lines"]
              and pixel_dash(pixels, scale, cell["bbox"])):
            cell["text"], cell["lines"], cell["source"] = "-", ["-"], "pixel_dash"


def disjoint_cells(boxes):
    """안쪽 작은 칸과 겹친 큰 칸을 직사각형 조각으로 나눈다. ㄴ자 머리글의 글자 중복 배정을 막는다."""
    result = []
    for box in boxes:
        parts = [box]
        inner = [b for b in boxes if b != box and base.contains(box, b, pad=0.1)]
        for cut in inner:
            remaining = []
            for part in parts:
                x0, y0, x1, y1 = part
                a, b, c, d = max(x0, cut[0]), max(y0, cut[1]), min(x1, cut[2]), min(y1, cut[3])
                if c <= a or d <= b:
                    remaining.append(part)
                    continue
                remaining.extend(p for p in ((x0, y0, a, y1), (c, y0, x1, y1),
                                              (a, y0, c, b), (a, d, c, y1))
                                 if p[2] - p[0] > SNAP and p[3] - p[1] > SNAP)
            parts = remaining
        result.extend(parts)
    return result


def nested_header_rows(boxes, children, pieces):
    """같은 2열 구성의 안쪽 표가 바깥 칸을 채우면, 안쪽 머리글 옆의 빈 띠도 바깥 행으로 남긴다."""
    if len(base.make_grid(boxes)[3]) != 3:
        return boxes
    for child in children:
        cells, _, cols, _ = base.make_grid(child)
        first = [c for c in cells if c["r"] == 0]
        if cols != 2 or len(first) != 2 or any(c["rs"] != 1 for c in first):
            continue
        top, bottom = first[0]["bbox"][1], first[0]["bbox"][3]
        if abs(bottom - first[1]["bbox"][3]) > CROSS:
            continue
        # 숫자 본문이 아닌, 짧은 머리글 두 칸인 경우만 쓴다.
        labels = [pieces_in(pieces, c["bbox"]) for c in first]
        if any(not group or any(base.is_numeric(p["text"]) for p in group)
               or len("".join(p["text"] for p in group)) > base.ROW_LABEL_MAX_CHARS for group in labels):
            continue
        child_bottom = max(b[3] for b in child)
        adjacent = [b for b in boxes if b[2] <= min(c[0] for c in child) and b[1] <= top <= b[1] + MIN_H_RUN
                    and abs(b[3] - child_bottom) <= CROSS and b[1] + CROSS < bottom < b[3] - MIN_H_RUN]
        for box in adjacent:
            band = (box[0], box[1], box[2], bottom)
            if pieces_in(pieces, band) or not pieces_in(pieces, (box[0], bottom, box[2], box[3])):
                continue
            boxes = [b for b in boxes if b != box] + [band, (box[0], bottom, box[2], box[3])]
            # 안쪽 표의 머리글을 바깥 값 칸이 다시 덮지 않도록 본문 시작점도 맞춘다.
            boxes = [(b[0], bottom, b[2], b[3]) if b[0] >= box[2] - CROSS
                     and abs(b[1] - box[1]) <= CROSS and abs(b[3] - box[3]) <= CROSS else b for b in boxes]
    return boxes


def ruled_tables(pixels, scale, pieces, page_number, counter):
    """이미지에서 찾은 선으로 표 격자를 만들고 OCR 글자를 칸에 넣는다. 규칙은 PDF 모드의 ruled_tables와 같다."""
    edges = detect_edges(pixels, scale, [p["bbox"] for p in pieces])
    cells_found = intersections_to_cells(edges_to_intersections(edges, CROSS, CROSS))
    found = [cells for cells in cells_to_tables(cells_found) if len(cells) > 1]
    boxes = [(min(c[0] for c in t), min(c[1] for c in t), max(c[2] for c in t), max(c[3] for c in t)) for t in found]

    def text_columns(cells):
        return {(round(b[0]), round(b[2])) for b in cells if pieces_in(pieces, b)}

    def parent_of(index):
        holders = [j for j, other in enumerate(boxes)
                   if j != index and base.contains(other, boxes[index]) and not base.contains(boxes[index], other)]
        return min(holders, key=lambda j: (boxes[j][2] - boxes[j][0]) * (boxes[j][3] - boxes[j][1])) if holders else None

    kept = [i for i, cells in enumerate(found)
            if len({round(c[0]) for c in cells}) > 1 and len(text_columns(cells)) > 1]
    tables = {}
    for i in kept:
        counter["table"] += 1
        tables[i] = {"id": f"t{counter['table']}", "type": "table", "kind": "ruled", "page": page_number,
                     "bbox": base.rounded(boxes[i]), "source_index": i}
    for i in kept:
        parent = parent_of(i)
        while parent is not None and parent not in tables:
            parent = parent_of(parent)
        tables[i]["parent_index"] = parent

    for i in kept:
        table = tables[i]
        children = [tables[j] for j in kept if tables[j]["parent_index"] == i]
        boxes_for_grid = nested_header_rows(disjoint_cells(found[i]),
                                           [found[j] for j in kept if tables[j]["parent_index"] == i], pieces)
        cells, n_rows, n_cols, xs = base.make_grid(boxes_for_grid)
        for child in children:
            if not any(base.overlap_area(cell["bbox"], child["bbox"]) for cell in cells):
                hole = base.grid_hole(cells, xs, child["bbox"])
                if hole:
                    cells.append(hole)
        cells.sort(key=lambda cell: (cell["r"], cell["c"]))
        holder_of = {}
        for child in children:
            best = max(range(len(cells)), key=lambda k: base.overlap_area(cells[k]["bbox"], child["bbox"]), default=None)
            if best is not None:
                holder_of.setdefault(best, []).append(child["id"])
        for k, cell in enumerate(cells):
            inner = [child["bbox"] for child in children if base.overlap_area(cell["bbox"], child["bbox"]) > 0]
            lines = [{"text": r["text"], "bbox": r["bbox"], "score": min(p["score"] for p in r["members"])}
                     for r in rows_of(pieces_in(pieces, cell["bbox"], exclude=inner))]
            cell["child_tables"] = holder_of.get(k, [])
            if cell["child_tables"]:
                notes, lines = base.split_notes(lines)
                cell["notes"] = notes
            cell["_lines"] = lines
            cell["lines"] = [line["text"] for line in lines]
            cell["text"] = " ".join(cell["lines"])
            cell["source"] = "ocr" if cell["text"] else None
            if lines:
                cell["ocr_score"] = round(min(line["score"] for line in lines), 4)
            cell["fill"] = cell_fill(pixels, scale, cell["bbox"])
        cells, regridded_rows = regrid_ocr(cells)
        if regridded_rows:
            n_rows = regridded_rows
            table["regridded"] = True
        table.update(n_rows=n_rows, n_cols=n_cols, col_edges=[round(x, 1) for x in xs])
        table["header_rule"] = base.mark_headers(cells)
        correct_single_cells(cells, pixels, scale)
        cells, table["n_rows"] = merge_split_row_labels(cells, table["n_rows"])
        table["header_rule"] = base.mark_headers(cells)
        for cell in cells:
            cell.pop("_lines", None)
        base.mark_row_labels(cells)
        base.header_paths(cells)
        table["cells"] = cells
    return [tables[i] for i in kept]


def correct_headers(tables, page_rows):
    """문서 안에서 더 자주 나온 머리글로 OCR 혼동만 고친다. 값 칸의 글자는 바꾸지 않는다."""
    def remember(cell, text):
        cell.setdefault("ocr_text", cell["text"])
        cell["text"], cell["lines"] = text, [text]

    # 원문자가 남은 같은 열의 순번으로 I/l/숫자 혼동을 확인한다. 빈칸은 추측해서 채우지 않는다.
    for table in tables:
        cells = table["cells"]
        for col in {c["c"] for c in cells if c["header"]}:
            labels = sorted((c for c in cells if c["c"] == col and c["cs"] == c["rs"] == 1), key=lambda c: c["r"])
            anchors = [(i, ord(c["text"]) - ord("①") + 1) for i, c in enumerate(labels)
                       if len(c["text"]) == 1 and "①" <= c["text"] <= "⑳"]
            offsets = {number - i for i, number in anchors}
            if len(offsets) != 1:
                continue
            offset = offsets.pop()
            for i, cell in enumerate(labels):
                number = i + offset
                if (cell["header"] and 1 <= number <= 20
                        and (cell["text"] == str(number) or (number == 1 and cell["text"] in {"I", "l", "Ⅰ"}))):
                    remember(cell, chr(ord("①") + number - 1))

    token = re.compile(r"[^\s()\[\]{}:;,./~%\-]+")
    counts = Counter()
    for rows in page_rows.values():
        for row in rows:
            counts.update(token.findall(base.squash(row["text"])))
    headers = [c for t in tables for c in t["cells"] if c["header"] and c["text"]]
    for cell in headers:
        counts.update(token.findall(base.squash(cell["text"])))

    def confusion(a, b):
        # 한 번의 흔한 OCR 오독만 허용한다. 실제 서로 다른 기수·숫자를 합치지 않는다.
        edits = [op for op in base.SequenceMatcher(None, a, b).get_opcodes() if op[0] != "equal"]
        if len(edits) != 1 or min(len(a), len(b)) < 3:
            return False
        kind, i, j, k, l = edits[0]
        old, new = a[i:j], b[k:l]
        groups = ({"기", "7", "71", "7I", "7l"}, {"0", "O", "o", "○"}, {"1", "I", "l", "Ⅰ"})
        if kind == "replace" and any(old in g and new in g for g in groups):
            return min(len(a) - len(old), len(b) - len(new)) >= 2
        # 같은 숫자를 한 번 더 읽은 경우. 올바른 숫자 머리글끼리는 보정하지 않는다.
        return (kind == "delete" and len(old) == 1 and old.isdigit() and not a.isdigit()
                and ((i and a[i - 1] == old) or (j < len(a) and a[j] == old))
                and not re.fullmatch(r".*\d+기", a))

    replacements = {}
    for cell in headers:
        text = cell["text"]
        for match in reversed(list(token.finditer(text))):
            word = match.group()
            if word not in replacements:
                candidates = [w for w, count in counts.items() if count > counts[word] and confusion(word, w)]
                best = max((counts[w] for w in candidates), default=0)
                winners = [w for w in candidates if counts[w] == best]
                replacements[word] = winners[0] if len(winners) == 1 else word
            text = text[:match.start()] + replacements[word] + text[match.end():]
        if text != cell["text"]:
            remember(cell, text)
    for table in tables:
        base.header_paths(table["cells"])
    # 안쪽 표의 문맥도 보정된 바깥 머리글에서 다시 얻는다.
    by_id = {t["id"]: t for t in tables}
    for table in tables:
        if table.get("parent"):
            parent = by_id[table["parent"]["table"]]
            holder = next(c for c in parent["cells"] if table["id"] in c.get("child_tables", []))
            table["context_path"] = holder.get("row_path", []) + holder.get("col_path", [])


def repeated_patterns(pages, page_rows):
    """여러 쪽의 위·아래 여백에 같은 글자(숫자는 무시)로 나오는 줄."""
    seen = defaultdict(set)
    for page in pages:
        for row in page_rows[page["page"]]:
            if row["bbox"][1] < page["height"] * base.MARGIN_RATIO:
                zone = "top"
            elif row["bbox"][3] > page["height"] * (1 - base.MARGIN_RATIO):
                zone = "bottom"
            else:
                continue
            seen[(zone, base.squash(re.sub(r"\d+", "#", row["text"])))].add(page["page"])
    need = max(3, len(pages) * REPEAT_PAGE_RATIO)
    return {key for key, found in seen.items() if len(found) >= need}


def build(layer1_path, layer1, ocr):
    ocr_pages = {p["page"]: [dict(line, text=clean_ocr_text(line["text"])) for line in p["lines"] if line["text"].strip()]
                 for p in ocr["pages"]}
    pages = [p for p in layer1["pages"] if p["page"] in ocr_pages]
    page_rows = {p["page"]: rows_of(ocr_pages[p["page"]]) for p in pages}
    repeated = repeated_patterns(pages, page_rows)
    rights = sorted(max(r["bbox"][2] for r in rows) for rows in page_rows.values() if rows)
    text_right = rights[len(rights) // 2] if rights else 0
    counter = Counter()
    nodes, tables, coverage = [], [], {}
    previous_toc = False
    for page in pages:
        number = page["page"]
        image = Image.open(layer1_path.parent / page["file"]).convert("RGB")
        pixels = np.asarray(image)
        scale = image.width / page["width"]
        # 반복 문구는 조각이 아니라 줄 단위로 판정한다
        # OCR마다 달라지는 머리말 띄어쓰기를 무시해 첫 표 앞에 반복 문단이 남지 않게 한다.
        dropped = {id(p) for r in page_rows[number]
                   if base.is_repeated(dict(r, text=base.squash(r["text"])), page["height"], repeated)
                   for p in r["members"]}
        pieces = [p for p in ocr_pages[number] if id(p) not in dropped]
        full_lines = rows_of(pieces)

        page_tables = ruled_tables(pixels, scale, pieces, number, counter)
        base.link_nested(page_tables)
        # 바깥 칸의 빈 띠로 돌출된 안쪽 머리글도 이미 표가 가진 글자다. 문단으로 중복 추출하지 않는다.
        cell_boxes = [c["bbox"] for t in page_tables for c in t["cells"]]
        free = [p for p in pieces if not any(base.inside(b, *base.center(p["bbox"])) for b in cell_boxes)]
        rows = rows_of(free)
        lines = [{"text": r["text"], "bbox": r["bbox"]} for r in rows]
        free_tables, used = base.aligned_tables(None, number, lines, counter,
                                                segs=[segments(r) for r in rows], source="ocr")
        for table in free_tables:
            cells, table["n_rows"] = merge_split_row_labels(table["cells"], table["n_rows"])
            for cell in cells:
                cell.pop("_lines", None)
            base.mark_aligned_headers(cells)
            base.header_paths(cells)
            table["cells"] = cells
        lines = [line for i, line in enumerate(lines) if i not in used]
        toc_page = base.is_toc_page(lines, number, len(pages), previous_toc)
        # PDF 모드와 같이 표에 들어간 목차도 줄로 되살린다.
        full_headings = sum(base.heading_kind(line["text"]) is not None for line in full_lines)
        free_headings = sum(base.heading_kind(line["text"]) is not None for line in lines)
        if (base.is_toc_page(full_lines, number, len(pages), previous_toc)
                and (not toc_page or full_headings >= base.TOC_PAGE_MIN_LINES and free_headings < full_headings / 2)):
            toc_page, lines, page_tables, free_tables = True, full_lines, [], []
        previous_toc = toc_page
        page_nodes = base.paragraphs(lines, page["width"], counter, number, text_right, toc_page)
        page_tables = page_tables + free_tables
        page_nodes.extend(page_tables)
        page_nodes.sort(key=lambda n: (n["bbox"][1], n["bbox"][0]))
        detect_table_units(page_tables, full_lines)
        nodes.extend(page_nodes)
        tables.extend(page_tables)

        # 빠진 글자 확인: 반복 문구를 뺀 OCR 글자가 결과에 모두 있는지
        source = Counter(base.squash("".join(p["text"] for p in pieces)))
        got = Counter(base.squash("".join(
            [n.get("raw", n.get("text", "")) for n in page_nodes if n["type"] != "table"]
            + [c.get("ocr_text", c["text"]) for t in page_tables for c in t["cells"]]
            + [note["text"] for t in page_tables for note in t["notes"]]
        )))
        lost = sum((source - got).values())
        if lost:
            coverage[str(number)] = lost
        print(f"  page {number}: tables={len(page_tables)} nodes={len(page_nodes)}", flush=True)

    correct_headers(tables, page_rows)
    nodes, sections = base.finalize(nodes, tables, counter, {p["page"]: p["height"] for p in pages},
                                    ocr_margins=True)
    inherit_table_units(nodes, tables)
    return {
        "structure_version": base.STRUCTURE_VERSION,
        "mode": "ocr_only",
        "source": layer1["source"],
        "document": base.document_info(nodes, tables),
        "sections": sections,
        "nodes": nodes,
        "check": {"missing_chars_by_page": coverage, "tables": len(tables), "ocr_pages": sorted(ocr_pages)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("layer1", type=Path)
    parser.add_argument("ocr", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    layer1 = json.loads(args.layer1.read_text(encoding="utf-8"))
    ocr = json.loads(args.ocr.read_text(encoding="utf-8"))
    result = build(args.layer1, layer1, ocr)
    output = args.output or args.layer1.with_name(args.layer1.stem + ".ocr_structure.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    kinds = Counter(n["type"] for n in result["nodes"])
    print(f"output={output} nodes={dict(kinds)} sections={len(result['sections'])} "
          f"check={result['check']['missing_chars_by_page'] or 'ok'} document={result['document']}")


if __name__ == "__main__":
    main()
