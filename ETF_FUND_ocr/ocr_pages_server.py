"""원격 PP-OCR 서버(/ppocr/ocr/pdf)에 PDF를 올려 쪽별 OCR 줄과 블록을 저장한다.

인증: 환경 변수 OCR_API_KEY (X-API-Key 헤더), OCR_BASIC_AUTH ("사용자:비밀번호", 터널 앞단 Basic 인증. 없으면 생략).

사용법:
    python ocr_pages_server.py 원본.pdf 추출.json [--pages 4,5] [--batch 20] [--server URL] [-o 출력.json]
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_SERVER = None  # 환경 변수 OCR_SERVER로 지정한다


LINE_GAP_RATIO = 0.7  # 표 밖: 위 줄과의 세로 간격이 글자 높이의 이 비율 이하일 때만 같은 블록
OVERLAP_RATIO = 0.5  # 표 밖: 가로로 좁은 쪽 폭의 이 비율 이상 겹치면 같은 열


def to_points(values, scale):
    return [round(v / scale, 1) for v in values]


def inside(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def center(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def reading_order(lines):
    """세로로 겹치는 줄은 같은 행으로 보고 왼쪽부터, 행은 위에서부터 놓는다."""
    rows = []
    for line in sorted(lines, key=lambda l: l["bbox"][1]):
        _, cy = center(line["bbox"])
        if rows and rows[-1][0]["bbox"][1] <= cy <= rows[-1][0]["bbox"][3]:
            rows[-1].append(line)
        else:
            rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda l: l["bbox"][0])]


def same_column(upper, lower):
    """lower가 upper 바로 아래에 이어지는 줄인지 bbox로 판단한다."""
    height = min(upper[3] - upper[1], lower[3] - lower[1])
    gap = lower[1] - upper[3]
    if gap < -height / 2 or gap > height * LINE_GAP_RATIO:
        return False
    overlap = min(upper[2], lower[2]) - max(upper[0], lower[0])
    narrower = min(upper[2] - upper[0], lower[2] - lower[0])
    return abs(upper[0] - lower[0]) <= height or overlap >= narrower * OVERLAP_RATIO


def group_lines(lines, regions):
    """표 영역은 칸 경계로, 표 밖은 줄 간격과 정렬로 OCR 줄을 블록으로 묶는다."""
    groups = {}  # 칸 키 -> 줄 목록
    free = []  # 표 칸에 속하지 않은 줄의 블록들
    for line in sorted(lines, key=lambda l: l["bbox"][1]):
        x, y = center(line["bbox"])
        key = next(
            (
                (region["id"], index)
                for region in regions
                for index, cell in enumerate(region.get("cells", []))
                if inside(cell, x, y)
            ),
            None,
        )
        if key:
            groups.setdefault(key, []).append(line)
            continue
        candidates = [block for block in free if same_column(block[-1]["bbox"], line["bbox"])]
        if candidates:
            min(candidates, key=lambda b: line["bbox"][1] - b[-1]["bbox"][3]).append(line)
        else:
            free.append([line])

    cells = {(r["id"], i): cell for r in regions for i, cell in enumerate(r.get("cells", []))}
    blocks = []
    for key, members in groups.items():
        blocks.append({"region_id": key[0], "cell": cells[key], "members": members})
    for members in free:
        x, y = center(members[0]["bbox"])
        region_id = next((r["id"] for r in regions if inside(r["bbox"], x, y)), None)
        blocks.append({"region_id": region_id, "cell": None, "members": members})

    result = []
    for block in blocks:
        members = reading_order(block["members"])
        boxes = [m["bbox"] for m in members]
        bbox = block["cell"] or [
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        ]
        result.append(
            {
                "type": "table" if block["region_id"] else "text",
                "region_id": block["region_id"],
                "bbox": bbox,
                "line_ids": [m["id"] for m in members],
                "text": "\n".join(m["text"] for m in members),
            }
        )
    return reading_order_blocks(result)


def reading_order_blocks(blocks):
    """블록도 줄과 같은 방식으로 위에서 아래, 같은 행은 왼쪽부터 놓는다."""
    ordered = reading_order(blocks)
    for index, block in enumerate(ordered):
        block["id"] = index
    return ordered


def page_result(page, lines, scale, seconds):
    """OCR 엔진과 무관하게 픽셀 좌표 줄 목록을 pt 좌표 줄과 블록으로 정리한다."""
    lines = [
        {"id": index, "text": line["text"], "score": round(line["score"], 4), "bbox": to_points(line["bbox"], scale)}
        for index, line in enumerate(lines)
    ]
    return {
        "page": page["page"],
        "width": page["width"],
        "height": page["height"],
        "file": page["file"],
        "seconds": seconds,
        "lines": lines,
        "blocks": group_lines(lines, page.get("table_regions", [])),
    }


def request(server, pdf_bytes, pages):
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"pages\"\r\n\r\n{','.join(map(str, pages))}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"document.pdf\"\r\n"
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + pdf_bytes + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "X-API-Key": os.environ["OCR_API_KEY"]}
    if os.environ.get("OCR_BASIC_AUTH"):
        headers["Authorization"] = "Basic " + base64.b64encode(os.environ["OCR_BASIC_AUTH"].encode()).decode()
    req = urllib.request.Request(f"{server}/ocr/pdf", data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        sys.exit(f"서버 오류 {error.code}: {error.read()[:300].decode(errors='replace')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("layer1", type=Path, help="쪽 크기(pt)와 이미지 경로를 얻는 1층 JSON")
    parser.add_argument("--pages", help="쉼표로 구분한 쪽 번호. 생략하면 전체")
    parser.add_argument("--batch", type=int, default=20, help="한 요청에 보낼 쪽 수")
    parser.add_argument("--server", default=os.environ.get("OCR_SERVER", DEFAULT_SERVER))
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    if not args.server:
        parser.error("OCR 서버 주소가 없습니다. 환경 변수 OCR_SERVER나 --server로 지정하세요.")

    data = json.loads(args.layer1.read_text(encoding="utf-8"))
    wanted = {int(p) for p in args.pages.split(",")} if args.pages else None
    pages = [p for p in data["pages"] if wanted is None or p["page"] in wanted]
    pdf_bytes = args.pdf.read_bytes()

    results, server_info = [], {}
    started = time.perf_counter()
    for start in range(0, len(pages), args.batch):
        chunk = pages[start:start + args.batch]
        mark = time.perf_counter()
        response = request(args.server, pdf_bytes, [p["page"] for p in chunk])
        server_info = {"dpi": response.get("dpi"), "page_count": response.get("page_count")}
        by_number = {p["page"]: p for p in response["pages"]}
        for page in chunk:
            got = by_number[page["page"]]
            # 서버 이미지 픽셀 → PDF pt
            result = page_result(page, got["lines"], got["width"] / page["width"], got.get("timing", {}).get("total"))
            result["server_timing"] = got.get("timing")
            results.append(result)
        print(f"  pages {chunk[0]['page']}-{chunk[-1]['page']}: {time.perf_counter() - mark:.1f}s {response.get('timing')}", flush=True)
    elapsed = time.perf_counter() - started

    output = args.output or args.layer1.with_name(args.layer1.stem + ".server_ocr.json")
    output.write_text(json.dumps({"source": data["source"], "engine": "server_ppocr", "server": server_info,
                                  "pages": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"output={output} pages={len(results)} total={elapsed:.1f}s per_page={elapsed / max(len(results), 1):.2f}s")


if __name__ == "__main__":
    main()
