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
import hashlib
import inspect
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
HOLE_PAD = 3  # pt. 빈 칸 자리에 넣을 OCR 글자 상자가 열 밖으로 넘쳐도 되는 폭
CONTINUE_EDGE = 2  # pt. 쪽을 넘어 이어진 표의 열 경계가 이만큼 안이면 같은 경계
JOIN = 3  # pt. 같은 선 위에서 이만큼 끊긴 것은 잇는다
MIN_EDGE = 4  # pt. 합친 뒤에도 이보다 짧은 선은 버린다
PARALLEL = 3  # pt. 굵은 선의 양쪽 경계나 이중선 사이 최대 거리 (경계 픽셀 포함)
ENDPOINT = 2  # pt. 이중선은 양 끝점까지 같아야 한다 (안쪽 표 테두리 보호)
ROW_OVERLAP = 0.5  # OCR 조각끼리 높이가 이 비율 이상 겹치면 같은 줄
FRAGMENT_CHARS = 30  # 값 줄에 붙일 조각 줄의 조각별 최대 글자 수
NUMBER_PIECE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?|-")
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


def row_runs(mask, min_len):
    """2차원 참/거짓 배열의 각 행에서 길이 min_len 이상인 참 구간 (행, 시작, 끝). 행 순서, 시작 순서로 나온다.

    runs()를 행마다 부른 것과 같은 결과를 배열 연산으로 한 번에 구한다.
    """
    rows, width = mask.shape
    padded = np.zeros((rows, width + 2), dtype=bool)
    padded[:, 1:-1] = mask
    change = np.flatnonzero(padded[:, 1:] != padded[:, :-1])
    starts, ends = change[0::2], change[1::2]
    keep = ends - starts >= min_len
    starts, ends = starts[keep], ends[keep]
    row = starts // (width + 1)
    return row, starts - row * (width + 1), ends - row * (width + 1)


def fill_runs(mask, row, start, end):
    """row_runs로 구한 구간을 mask에 참으로 칠한다."""
    rows, width = mask.shape
    # 한 행의 구간은 서로 겹치지 않아 누적합이 0 또는 1이다 (int8로 충분하다).
    marks = np.zeros((rows, width + 1), dtype=np.int8)
    marks[row, start] += 1
    marks[row, end] -= 1
    mask |= np.cumsum(marks, axis=1, dtype=np.int8)[:, :width] > 0


def channel_max(a):
    return np.maximum(np.maximum(a[..., 0], a[..., 1]), a[..., 2])


def pixel_edges(pixels, scale):
    """쪽 이미지 픽셀만으로 찾은 선 조각 (픽셀 정수 배열: 방향 0=가로/1=세로, 위치, 시작, 끝).

    이미지·배율이 같으면 결과가 같아 캐시에 저장해 다시 쓴다.
    """
    color = pixels.astype(np.int16)
    step_h = np.zeros(pixels.shape[:2], dtype=np.int16)
    step_v = np.zeros(pixels.shape[:2], dtype=np.int16)
    step_h[1:-1] = channel_max(np.abs(color[2:] - color[:-2]))
    step_v[:, 1:-1] = channel_max(np.abs(color[:, 2:] - color[:, :-2]))
    k = max(2, round(LINE_INTERIOR * scale))
    light = np.minimum(np.minimum(pixels[..., 0], pixels[..., 1]), pixels[..., 2]).astype(np.int16)
    lu, ld, ll, lr = np.full_like(light, 255), np.full_like(light, 255), np.full_like(light, 255), np.full_like(light, 255)
    lu[k:], ld[:-k] = light[:-k], light[k:]
    ll[:, k:], lr[:, :-k] = light[:, :-k], light[:, k:]
    horizontal = (step_h > COLOR_STEP) | (light < np.minimum(lu, ld) - LINE_CONTRAST)
    vertical = (step_v > COLOR_STEP) | (light < np.minimum(ll, lr) - LINE_CONTRAST)

    # 옅은 면 경계는 긴 구간만 남겨 글자 가장자리의 안티앨리어싱과 구별한다.
    faint_h, faint_v = step_h > FILL_STEP, step_v > FILL_STEP
    fill_runs(horizontal, *row_runs(faint_h, MIN_FILL_H_RUN * scale))
    vertical_t = vertical.T.copy()
    fill_runs(vertical_t, *row_runs(faint_v.T, MIN_FILL_V_RUN * scale))

    h_row, h_start, h_end = row_runs(horizontal, MIN_H_RUN * scale)
    v_col, v_start, v_end = row_runs(vertical_t, MIN_V_RUN * scale)
    found = np.concatenate([
        np.stack([np.zeros_like(h_row), h_row, h_start, h_end], axis=1),
        np.stack([np.ones_like(v_col), v_col, v_start, v_end], axis=1)])
    return found.astype(np.int32).reshape(-1, 4)


def edges_from_pixels(found, scale):
    """pixel_edges의 정수 배열을 pdfplumber 형식의 선 목록(pt 단위)으로 바꾼다."""
    edges = []
    for kind, at, a, b in found.tolist():
        if kind == 0:
            edges.append({"x0": a / scale, "x1": b / scale, "top": at / scale, "bottom": at / scale,
                          "width": (b - a) / scale, "height": 0, "orientation": "h"})
        else:
            edges.append({"x0": at / scale, "x1": at / scale, "top": a / scale, "bottom": b / scale,
                          "width": 0, "height": (b - a) / scale, "orientation": "v"})
    return edges


def edge_cache_key(image_path, scale):
    """이미지 내용, 배율, 선 찾기 코드·상수가 모두 같을 때만 캐시를 쓴다."""
    code = "".join(inspect.getsource(f) for f in (pixel_edges, row_runs, fill_runs, channel_max)) + repr(
        (COLOR_STEP, FILL_STEP, LINE_INTERIOR, LINE_CONTRAST, MIN_H_RUN, MIN_V_RUN, MIN_FILL_H_RUN, MIN_FILL_V_RUN))
    digest = hashlib.sha1(Path(image_path).read_bytes())
    digest.update(repr(scale).encode())
    digest.update(code.encode())
    return digest.hexdigest()


def detect_edges(pixels, scale, text_boxes, raw=None):
    """쪽 이미지에서 표 선 후보를 찾는다.

    색이 크게 바뀌는 경계(흰 바탕↔면, 면↔다른 색 면, 선의 양 가장자리)와, 양쪽보다 확실히 어두운 얇은 줄
    (배경색 위에 그은 선)을 선 픽셀로 본다. 옅은 면 경계는 긴 구간만 남기는 방향별 열기로 추출한다.
    OCR 상자에 가려진 틈을 잇고, 양 끝점까지 같은 굵은 선·이중선의 경계를 중심선으로 줄인다.
    """
    edges = edges_from_pixels(raw if raw is not None else pixel_edges(pixels, scale), scale)
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


def attach_row_fragments(rows):
    """선 없는 표에서 값 줄과 반쯤 겹친 짧은 조각 줄(두 줄로 감긴 행 이름, 병합 칸의 날짜)을 그 값 줄에 붙인다.

    rows_of는 높이가 ROW_OVERLAP 이상 겹칠 때만 같은 줄로 본다. 두 줄 행 이름은 값 줄 위아래로 조금씩만 겹쳐
    따로 줄이 되고, 겹친 줄 때문에 선 없는 표가 끊긴다. 숫자 조각이 3개 이상인 줄과 겹치고 가로로는
    그 줄의 어떤 조각과도 부딪히지 않는 짧은 줄만 붙인다. 값 줄의 높이는 그대로 둔다.
    """
    def numbers(row):
        return sum(bool(NUMBER_PIECE.fullmatch(p["text"].strip())) for p in row["members"])

    def overlap(a, b):
        return min(a["bbox"][3], b["bbox"][3]) - max(a["bbox"][1], b["bbox"][1])

    targets = [r for r in rows if numbers(r) >= 3]
    # 가로 충돌은 값 줄의 원래 조각과만 본다. 같은 열에 쌓인 감긴 행 이름 두 줄을 모두 붙이기 위해서다.
    original = {id(t): list(t["members"]) for t in targets}
    target_ids = {id(r) for r in targets}
    merged, grown = set(), set()
    for row in rows:
        # 숫자 조각은 붙이지 않는다 (그래프 눈금·값 칸 글자). 행 이름과 날짜 같은 글자 조각만 붙인다.
        if id(row) in target_ids or numbers(row) >= 1 or len(row["members"]) > 2 \
                or any(len(p["text"].strip()) > FRAGMENT_CHARS for p in row["members"]):
            continue
        # 붙인 조각이 값 줄의 옆 칸 글자와 한 칸으로 합쳐지지 않을 만큼 떨어져 있어야 한다 (segments 기준).
        near = [t for t in targets if overlap(row, t) > 0 and not any(
            min(p["bbox"][2], q["bbox"][2]) + (t["bbox"][3] - t["bbox"][1]) * base.ALIGNED_GAP_RATIO
            > max(p["bbox"][0], q["bbox"][0])
            for p in row["members"] for q in original[id(t)])]
        if not near:
            continue
        target = max(near, key=lambda t: overlap(row, t))
        target["members"].extend(row["members"])
        merged.add(id(row))
        grown.add(id(target))
    out = []
    for row in rows:
        if id(row) in merged:
            continue
        if id(row) in grown:
            # 같은 열에 쌓인 조각(감긴 행 이름)은 위에서 아래 순서로, 나머지는 왼쪽부터 둔다.
            ordered, column = [], []
            for piece in sorted(row["members"], key=lambda p: p["bbox"][0]):
                if column and piece["bbox"][0] < max(q["bbox"][2] for q in column):
                    column.append(piece)
                else:
                    ordered.extend(sorted(column, key=lambda q: q["bbox"][1]))
                    column = [piece]
            ordered.extend(sorted(column, key=lambda q: q["bbox"][1]))
            row["members"] = ordered
            row["text"] = " ".join(p["text"].strip() for p in ordered).strip()
        out.append(row)
    return out


def split_period_cells(cells, col_edges):
    """선 없는 표에서 여러 열에 걸친 기간 머리글 칸을 열마다 나눈다.

    OCR이 '최근1년 최근 2년 … 설정일 이후'나 '25.04.15 24.04.15 …'를 한 조각으로 읽은 경우다.
    기간 토막 수가 걸친 열 수와 정확히 같을 때만 나눈다.
    """
    out = []
    for cell in cells:
        tokens = re.findall(base.PERIOD_TOKEN, cell["text"])
        if cell["cs"] > 1 and len(tokens) == cell["cs"] and base.PERIOD_HEADER.fullmatch(cell["text"]):
            for k, token in enumerate(tokens):
                c = cell["c"] + k
                out.append(dict(cell, c=c, cs=1, text=token.strip(), lines=[token.strip()],
                                bbox=base.rounded((col_edges[c], cell["bbox"][1], col_edges[c + 1], cell["bbox"][3]))))
        else:
            out.append(cell)
    return out


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


def split_merged_headers(cells, xs, pieces):
    """Split only independently aligned OCR columns above an actual value row.

    A single line crossing an edge is a genuine spanning heading. Two or more
    stacked, unspaced OCR fragments may be cut at a character boundary when
    the OCR engine itself joined adjacent labels.
    """
    candidates = []
    for cell in sorted(cells, key=lambda c: (c["r"], c["c"])):
        if candidates:
            prior, originals = candidates[-1]
            if (cell["text"] and cell.get("header") and prior.get("header")
                    and cell["cs"] == 1 and originals[-1]["cs"] == 1
                    and not cell.get("child_tables") and not prior.get("child_tables")
                    and cell["text"] == prior["text"] and cell["r"] == prior["r"] and cell["rs"] == prior["rs"]
                    and cell["c"] == prior["c"] + prior["cs"]):
                # A fragment centred on a ruling can have been copied into
                # both cells. Reconstruct its span before assigning characters.
                candidates[-1] = (dict(prior, cs=prior["cs"] + 1,
                                       bbox=[prior["bbox"][0], prior["bbox"][1], cell["bbox"][2], prior["bbox"][3]]),
                                  originals + [cell])
                continue
        candidates.append((cell, [cell]))
    result = []
    for cell, originals in candidates:
        cols = range(cell["c"], cell["c"] + cell["cs"])
        if (cell["cs"] <= 1 or not cell.get("header") or cell.get("child_tables")
                or not cell["text"] or base.is_numeric(cell["text"])):
            result.extend(originals)
            continue
        below = [c for c in cells if c["r"] >= cell["r"] + cell["rs"]
                 and c["cs"] == 1 and c["c"] in cols and base.is_numeric(c["text"])]
        rows = {c["r"] for c in below}
        value_cols = set(cols) - {0}  # A collapsed header can include the row-label column.
        body_rows = [r for r in rows if {c["c"] for c in below if c["r"] == r} >= value_cols]
        if not value_cols or not body_rows:
            result.extend(originals)
            continue
        if any(c["cs"] == 1 and c["c"] in value_cols and c["text"] and not base.is_numeric(c["text"])
               and cell["r"] + cell["rs"] <= c["r"] < min(body_rows) for c in cells):
            # Specific subheaders already explain the columns: keep the
            # shared upper heading, even when it wraps into several lines.
            result.extend(originals)
            continue
        cell_pieces = pieces_in(pieces, cell["bbox"])
        ocr_rows = rows_of(cell_pieces)
        if len(ocr_rows) > 1 and len(ocr_rows[0]["members"]) == 1:
            top = ocr_rows[0]["bbox"]
            width = cell["bbox"][2] - cell["bbox"][0]
            if top[2] - top[0] < width * .4 and abs(base.center(top)[0] - base.center(cell["bbox"])[0]) < width * .1:
                # A lone centred upper word can name all the subcolumns.
                # Assigning it to the middle column would lose that meaning.
                result.extend(originals)
                continue
        groups, crossing, valid = defaultdict(list), 0, True
        for piece in cell_pieces:
            b, text = piece["bbox"], piece["text"]
            col = next((c for c in cols if xs[c] <= base.center(b)[0] < xs[c + 1]), None)
            if col is None:
                valid = False
                break
            cuts = [x for x in xs[cell["c"] + 1:cell["c"] + cell["cs"]]
                    if b[0] + 1.5 < x < b[2] - 1.5]
            if not cuts:
                groups[col].append(piece)
                continue
            crossing += 1
            if re.search(r"\s|[()%,.:：]", text):
                valid = False
                break
            indices = [(x - b[0]) / (b[2] - b[0]) * len(text) for x in cuts]
            if any(abs(i - round(i)) > .25 or not 0 < round(i) < len(text) for i in indices):
                valid = False
                break
            ends = [0, *map(round, indices), len(text)]
            edges = [b[0], *cuts, b[2]]
            spans = [(next((c for c in cols if xs[c] <= (left + right) / 2 < xs[c + 1]), None), left, right, start, end)
                     for left, right, start, end in zip(edges, edges[1:], ends, ends[1:])]
            if any(c is None for c, *_ in spans):
                # 조각이 칸 밖으로 삐져나와 나눈 조각 하나가 어느 열에도 들지 않으면 나누지 않는다.
                valid = False
                break
            for c, left, right, start, end in spans:
                groups[c].append(dict(piece, text=text[start:end], bbox=[left, b[1], right, b[3]]))
        if not valid or (crossing and crossing < 2) or set(groups) != set(cols):
            result.extend(originals)
            continue
        if any(len(base.squash("".join(p["text"] for p in group))) < 2 for group in groups.values()):
            # Widely spaced letters such as "금 액" are one shared word,
            # not evidence of independent column headings.
            result.extend(originals)
            continue
        for col in cols:
            lines = rows_of(groups[col])
            texts = [line["text"] for line in lines]
            new = dict(cell, c=col, cs=1, bbox=[xs[col], cell["bbox"][1], xs[col+1], cell["bbox"][3]],
                       text=" ".join(texts), lines=texts, _lines=lines)
            new.pop("ocr_text", None)
            result.append(new)
    return sorted(result, key=lambda c: (c["r"], c["c"]))


def split_merged_rows(cells, n_rows, pieces):
    """Require matching numeric lines in at least two columns before splitting."""
    result, shifts = [], {}
    dots = re.compile(r"^[\s.…·ㆍ⋯・]+$")
    for r in range(n_rows):
        if any(c["r"] == r and c.get("child_tables") for c in cells):
            continue
        if any(c["text"] and c["rs"] > 1 and c["r"] <= r < c["r"] + c["rs"] for c in cells):
            # Do not expand a spanning subtotal into newly inserted rows.
            # Ambiguous ruled rows are left to the existing OCR regridding.
            continue
        row = [c for c in cells if c["r"] == r and c["rs"] == 1 and not c.get("child_tables")]
        parts = {id(c): [p for p in pieces_in(pieces, c["bbox"]) if not dots.fullmatch(p["text"])] for c in row}
        numeric = [parts[id(c)] for c in row if len(parts[id(c)]) >= 2
                   and all(base.is_numeric(p["text"]) for p in parts[id(c)])]
        if len(numeric) < 2:
            continue
        heights = sorted(p["bbox"][3] - p["bbox"][1] for group in numeric for p in group)
        tolerance = heights[len(heights)//2] * .4
        ys = base.cluster([base.center(p["bbox"])[1] for group in numeric for p in group], tolerance=tolerance)
        if len(ys) < 2 or any(len(group) != len(ys) for group in numeric):
            continue
        grouped = {}
        valid = True
        for c in row:
            groups = defaultdict(list)
            for p in parts[id(c)]:
                y = base.center(p["bbox"])[1]
                i = base.nearest(y, ys)
                if abs(y - ys[i]) > tolerance:
                    valid = False
                    break
                groups[i].append(p)
            if c["text"] and set(groups) != set(range(len(ys))):
                valid = False
            grouped[id(c)] = groups
        if not valid:
            continue
        shifts[r] = len(ys) - 1
        for c in row:
            boundaries = [c["bbox"][1], *[(a+b)/2 for a,b in zip(ys, ys[1:])], c["bbox"][3]]
            for i in range(len(ys)):
                lines = rows_of(grouped[id(c)][i])
                texts = [line["text"] for line in lines]
                result.append(dict(c, r=r, _subrow=i, bbox=[c["bbox"][0], boundaries[i], c["bbox"][2], boundaries[i+1]],
                                   text=" ".join(texts), lines=texts, _lines=lines))
    for c in cells:
        if c["r"] not in shifts or c["rs"] != 1:
            result.append(dict(c))
    for c in result:
        r = c["r"]
        c["r"] += sum(n for old, n in shifts.items() if old < r) + c.pop("_subrow", 0)
        c["rs"] += sum(n for old, n in shifts.items() if r <= old < r + c["rs"]) if c["rs"] > 1 else 0
    return sorted(result, key=lambda c: (c["r"], c["c"])), n_rows + sum(shifts.values())


def merge_wrapped_header_dates(cells, n_rows):
    """Join only a date-range start and its adjacent date continuation."""
    start = re.compile(r"^\(?\s*\d{2,4}[./-]\d{1,2}[./-]\d{1,2}\s*[~～〜–-]\s*$")
    end = re.compile(r"^\s*\d{2,4}[./-]\d{1,2}[./-]\d{1,2}\s*\)?\s*$")
    removed, joined = set(), {}
    for r in range(n_rows - 1):
        upper = [c for c in cells if c["r"] == r and c["rs"] == 1 and c["text"]]
        lower = [c for c in cells if c["r"] == r + 1 and c["text"]]
        if (not upper or len(upper) != len(lower)
                or not all(c["header"] and start.fullmatch(c["text"]) for c in upper)
                or not all(c["header"] and c["rs"] == 1 and end.fullmatch(c["text"]) for c in lower)):
            continue
        pairs = [(c, next((d for d in lower if (d["c"], d["cs"]) == (c["c"], c["cs"])), None)) for c in upper]
        if any(d is None for _, d in pairs):
            continue
        for c, d in pairs:
            joined[id(c)] = dict(c, text=c["text"] + " " + d["text"], lines=c["lines"] + d["lines"],
                                 _lines=c["_lines"] + d["_lines"],
                                 bbox=[min(c["bbox"][0], d["bbox"][0]), c["bbox"][1],
                                       max(c["bbox"][2], d["bbox"][2]), d["bbox"][3]])
        removed.add(r + 1)
    result = []
    for cell in cells:
        if cell["r"] in removed:
            continue
        c = dict(joined.get(id(cell), cell))
        c["rs"] -= sum(c["r"] <= r < c["r"] + c["rs"] for r in removed)
        c["r"] -= sum(r < c["r"] for r in removed)
        result.append(c)
    return result, n_rows - len(removed)


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
            count = sum(base.is_numeric(c.get("ocr_text", c["text"])) for c in peers)
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


# 보수표 머리글에 쓰이는 역할 이름. OCR이 이웃 칸의 머리글을 한 줄로 붙여 읽은 경우만 나눈다.
FEE_HEADER_WORDS = ("집합투자업자", "판매회사", "신탁업자", "일반사무관리회사", "지정참가회사")


def fee_header_token(text):
    """머리글 한 칸 한 줄로 쓸 수 있는 말: '보수', 역할 이름 전체·앞·뒤 2글자 이상(+보수)."""
    if text == "보수":
        return True
    core = text[:-2] if text.endswith("보수") and len(text) > 2 else text
    return len(core) >= 2 and any(w.startswith(core) or w.endswith(core) for w in FEE_HEADER_WORDS)


def fee_header_parts(text, count):
    """공백 없는 줄을 머리글 말 count개로 나누는 방법이 딱 하나일 때만 그 조각들을 돌려준다."""
    found = []

    def walk(rest, parts):
        if len(found) > 1 or len(parts) > count:
            return
        if not rest:
            if len(parts) == count:
                found.append(parts)
            return
        for k in range(2, len(rest) + 1):
            if fee_header_token(rest[:k]):
                walk(rest[k:], parts + [rest[:k]])

    if "|" in text:
        parts = [s.strip() for s in text.split("|")]
        return parts if len(parts) == count and all(fee_header_token(s) for s in parts) else None
    walk(text, [])
    if len(found) != 1:
        return None
    whole = set(FEE_HEADER_WORDS) | {w + "보수" for w in FEE_HEADER_WORDS}
    # 역할 이름 하나가 두 칸에 걸친 것(병합 머리글)은 나누지 않는다.
    if any(a + b in whole for a, b in zip(found[0], found[0][1:])):
        return None
    return found[0]


def split_fee_header_pieces(pieces, cells):
    """선으로 나뉜 여러 칸에 걸친 OCR 줄을, 보수 머리글 말로만 이루어지고 말 수가 걸친 칸 수와 같을 때만 칸마다 나눈다.

    좌표는 어느 칸들에 걸쳤는지와 그 순서를 알 때만 쓴다. 글자를 어디서 자를지는 말 목록으로 정한다.
    """
    out = []
    for p in pieces:
        b = p["bbox"]
        cy = (b[1] + b[3]) / 2
        crossed = sorted((c for c in cells if c["bbox"][1] <= cy < c["bbox"][3]
                          and min(b[2], c["bbox"][2]) - max(b[0], c["bbox"][0]) > 1.5),
                         key=lambda c: c["bbox"][0])
        text = re.sub(r"\s+", "", p["text"])
        parts = fee_header_parts(text, len(crossed)) if len(crossed) > 1 and not re.search(r"\d", text) else None
        if not parts:
            out.append(p)
            continue
        for part, c in zip(parts, crossed):
            box = [max(b[0], c["bbox"][0]), b[1], min(b[2], c["bbox"][2]), b[3]]
            out.append(dict(p, text=part, bbox=box, split_from=p["text"]))
    return out


def text_holes(cells, xs, pieces, exclude):
    """격자 안인데 칸이 만들어지지 않은 자리에 OCR 글자가 있으면 그 자리에 칸을 만든다.

    맨 왼쪽 행 이름 열이나 맨 오른쪽 열의 가로선을 못 찾으면 pdfplumber가 그 열의 칸을 만들지 않아,
    표 테두리 안의 글자가 표 밖 문단으로 빠진다. 다른 열이 정한 행 경계와 열 경계로 칸을 만든다.
    """
    tops = {cell["r"]: cell["bbox"][1] for cell in cells}
    bottoms = {cell["r"] + cell["rs"]: cell["bbox"][3] for cell in cells}
    edges = {**tops, **bottoms}
    covered = {(r, c) for cell in cells for r in range(cell["r"], cell["r"] + cell["rs"])
               for c in range(cell["c"], cell["c"] + cell["cs"])}
    added = []
    for row in sorted(edges):
        if row + 1 not in edges:
            continue
        for col in range(len(xs) - 1):
            box = base.rounded((xs[col], edges[row], xs[col + 1], edges[row + 1]))
            inside = pieces_in(pieces, box, exclude=exclude) if (row, col) not in covered else []
            # 글자가 그 열 폭 안에 들어갈 때만 만든다. 여러 열에 걸친 문장은 원래대로 문단으로 둔다.
            # OCR 글자 상자는 실제 글자보다 조금 넓게 잡히므로 HOLE_PAD만큼은 넘쳐도 된다.
            if inside and all(box[0] - HOLE_PAD <= q["bbox"][0] and q["bbox"][2] <= box[2] + HOLE_PAD for q in inside):
                added.append({"r": row, "c": col, "rs": 1, "cs": 1, "bbox": box})
    return added


def restore_outer_columns(cells, xs, prior_xs, pieces, box):
    """앞 쪽 표에서 이어진 표가 바깥 열의 세로선을 못 찾아 안쪽 열만 남은 경우 앞 표의 열 경계로 되살린다.

    열 경계가 앞 표 열 경계의 연속된 일부와 같고, 빠진 바깥 띠에 표 높이 안의 OCR 글자가 있을 때만 늘린다.
    늘린 열의 칸은 text_holes가 다른 열의 행 경계로 만든다.
    """
    if not prior_xs or len(xs) < 3 or len(prior_xs) <= len(xs):
        return xs, 0
    for k in range(len(prior_xs) - len(xs) + 1):
        if all(abs(x - y) <= CONTINUE_EDGE for x, y in zip(xs, prior_xs[k:k + len(xs)])):
            break
    else:
        return xs, 0

    def has_text(x0, x1):
        return any(x0 - CROSS <= q["bbox"][0] and q["bbox"][2] <= x1 + CROSS
                   and box[1] <= base.center(q["bbox"])[1] <= box[3] for q in pieces)

    left = prior_xs[:k] if k and has_text(prior_xs[0], xs[0]) else []
    right = prior_xs[k + len(xs):]
    right = right if right and has_text(xs[-1], right[-1]) else []
    if left:
        for cell in cells:
            cell["c"] += len(left)
    return list(left) + list(xs) + list(right), len(left)


def ruled_tables(pixels, scale, pieces, page_number, counter, raw_edges=None, prior_xs=None):
    """이미지에서 찾은 선으로 표 격자를 만들고 OCR 글자를 칸에 넣는다. 규칙은 PDF 모드의 ruled_tables와 같다."""
    edges = detect_edges(pixels, scale, [p["bbox"] for p in pieces], raw_edges)
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

    roots = [i for i in kept if tables[i]["parent_index"] is None]
    top = min(roots, key=lambda i: boxes[i][1], default=None)
    for i in kept:
        table = tables[i]
        children = [tables[j] for j in kept if tables[j]["parent_index"] == i]
        boxes_for_grid = nested_header_rows(disjoint_cells(found[i]),
                                           [found[j] for j in kept if tables[j]["parent_index"] == i], pieces)
        cells, n_rows, n_cols, xs = base.make_grid(boxes_for_grid)
        # 쪽 맨 위 표만 앞 쪽 표의 열 경계로 바깥 열을 되살린다.
        if i == top and len(xs) > 2:
            restored = restore_outer_columns(cells, xs, prior_xs, pieces, boxes[i])[0]
            if len(restored) != len(xs):
                xs, n_cols = restored, len(restored) - 1
                table["bbox"] = base.rounded((min(xs[0], boxes[i][0]), boxes[i][1], max(xs[-1], boxes[i][2]), boxes[i][3]))
        for child in children:
            if not any(base.overlap_area(cell["bbox"], child["bbox"]) for cell in cells):
                hole = base.grid_hole(cells, xs, child["bbox"])
                if hole:
                    cells.append(hole)
        cells.extend(text_holes(cells, xs, pieces, [child["bbox"] for child in children]))
        cells.sort(key=lambda cell: (cell["r"], cell["c"]))
        table_pieces = split_fee_header_pieces(pieces_in(pieces, table["bbox"]), cells)
        holder_of = {}
        for child in children:
            best = max(range(len(cells)), key=lambda k: base.overlap_area(cells[k]["bbox"], child["bbox"]), default=None)
            if best is not None:
                holder_of.setdefault(best, []).append(child["id"])
        for k, cell in enumerate(cells):
            inner = [child["bbox"] for child in children if base.overlap_area(cell["bbox"], child["bbox"]) > 0]
            lines = [{"text": r["text"], "bbox": r["bbox"], "score": min(p["score"] for p in r["members"])}
                     for r in rows_of(pieces_in(table_pieces, cell["bbox"], exclude=inner))]
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
        cells, n_rows = split_merged_rows(cells, n_rows, table_pieces)
        base.mark_headers(cells)
        cells, n_rows = merge_wrapped_header_dates(cells, n_rows)
        table.update(n_rows=n_rows, n_cols=n_cols, col_edges=[round(x, 1) for x in xs])
        table["header_rule"] = base.mark_headers(cells)
        correct_single_cells(cells, pixels, scale)
        cells = split_merged_headers(cells, xs, table_pieces)
        cells, table["n_rows"] = merge_split_row_labels(cells, table["n_rows"])
        table["header_rule"] = base.mark_headers(cells)
        correct_single_cells(cells, pixels, scale)
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
                # 허용하는 오독은 한 번뿐이라 길이가 2 이상 다르면 비교하지 않는다 (결과는 같고 빠르다).
                candidates = [w for w, count in counts.items() if count > counts[word]
                              and abs(len(w) - len(word)) <= 1 and confusion(word, w)]
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
    # 쪽 이미지의 선 찾기 결과 캐시. 후보정 규칙만 바꿨을 때 이미지 분석을 다시 하지 않는다.
    cache_path = layer1_path.with_name(layer1_path.stem + ".edges_cache.npz")
    edge_cache = {}
    try:
        with np.load(cache_path, allow_pickle=False) as saved:
            for name in saved.files:
                if name.endswith("|edges"):
                    file = name[:-len("|edges")]
                    edge_cache[file] = {"key": str(saved[file + "|key"]), "edges": saved[name]}
    except (OSError, ValueError, KeyError):
        edge_cache = {}
    cache_changed = False
    prior_xs, prior_page = None, None
    for page in pages:
        number = page["page"]
        image_path = layer1_path.parent / page["file"]
        image = Image.open(image_path).convert("RGB")
        pixels = np.asarray(image)
        scale = image.width / page["width"]
        key = edge_cache_key(image_path, scale)
        cached = edge_cache.get(page["file"])
        if not cached or cached.get("key") != key:
            cached = edge_cache[page["file"]] = {"key": key, "edges": pixel_edges(pixels, scale)}
            cache_changed = True
        # 반복 문구는 조각이 아니라 줄 단위로 판정한다
        # OCR마다 달라지는 머리말 띄어쓰기를 무시해 첫 표 앞에 반복 문단이 남지 않게 한다.
        dropped = {id(p) for r in page_rows[number]
                   if base.is_repeated(dict(r, text=base.squash(r["text"])), page["height"], repeated)
                   for p in r["members"]}
        pieces = [p for p in ocr_pages[number] if id(p) not in dropped]
        full_lines = rows_of(pieces)

        page_tables = ruled_tables(pixels, scale, pieces, number, counter, cached["edges"],
                                   prior_xs=prior_xs if prior_page == number - 1 else None)
        outer = [t for t in page_tables if t.get("parent_index") is None]
        if outer:
            prior_xs, prior_page = max(outer, key=lambda t: t["bbox"][3])["col_edges"], number
        base.link_nested(page_tables)
        # 바깥 칸의 빈 띠로 돌출된 안쪽 머리글도 이미 표가 가진 글자다. 문단으로 중복 추출하지 않는다.
        cell_boxes = [c["bbox"] for t in page_tables for c in t["cells"]]
        free = [p for p in pieces if not any(base.inside(b, *base.center(p["bbox"])) for b in cell_boxes)]
        rows = attach_row_fragments(rows_of(free))
        lines = [{"text": r["text"], "bbox": r["bbox"]} for r in rows]
        free_tables, used = base.aligned_tables(None, number, lines, counter,
                                                segs=[segments(r) for r in rows], source="ocr")
        for table in free_tables:
            cells, table["n_rows"] = merge_split_row_labels(split_period_cells(table["cells"], table["col_edges"]),
                                                            table["n_rows"])
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
        for table in page_tables:
            above = [line for line in full_lines if 0 <= table["bbox"][1] - line["bbox"][3] < 40]
            if above:
                title = max(above, key=lambda line: line["bbox"][3])
                if "수익률" in title["text"] and re.search(r"기준일\s*[:：]", title["text"]):
                    table.setdefault("caption", title["text"])
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

    if cache_changed:
        live = {page["file"] for page in pages}
        arrays = {}
        for file, item in edge_cache.items():
            if file in live:
                arrays[file + "|key"], arrays[file + "|edges"] = np.array(item["key"]), item["edges"]
        with open(cache_path, "wb") as handle:
            np.savez_compressed(handle, **arrays)
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
