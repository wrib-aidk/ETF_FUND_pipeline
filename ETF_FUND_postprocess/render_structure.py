"""2층 결과(<stem>.structure.json)를 사람이 검토할 수 있는 HTML로 그린다.

섹션 트리, 문단, 병합 칸이 살아 있는 표(안쪽 표는 바깥 칸 안에), 표마다 값과 머리글 경로를 보여 준다.

사용법:
    python render_structure.py 구조.json [-o 출력.html]
"""
import argparse
import json
from html import escape
from pathlib import Path

CSS = """
body{margin:0;font:14px/1.6 'Malgun Gothic',sans-serif;color:#1d1d1f;background:#f6f6f4}
nav{position:fixed;top:0;left:0;bottom:0;width:300px;overflow:auto;background:#fff;border-right:1px solid #ddd;padding:12px 10px;font-size:12.5px}
nav a{color:#333;text-decoration:none;display:block;padding:1px 0}nav a:hover{color:#0a58ca}
nav .l1{font-weight:700;margin-top:8px}nav .l2{padding-left:12px}nav .l3{padding-left:24px;color:#666}nav .l4{padding-left:36px;color:#888}
main{margin-left:320px;max-width:980px;padding:20px 28px 80px}
.info{background:#fff;border:1px solid #ddd;border-radius:6px;padding:10px 14px;margin-bottom:16px}
.page{color:#999;font-size:11px;border-top:1px dashed #ccc;margin:22px 0 6px;padding-top:2px}
h1{font-size:21px;border-bottom:2px solid #333;padding-bottom:4px}h2{font-size:17px}h3{font-size:15px}h4{font-size:14px}
.li{padding-left:14px;color:#333}p{margin:4px 0}
.toc{color:#666;font-size:12.5px;margin:1px 0 1px 10px}
.tbl{margin:10px 0 14px}.meta{font-size:11.5px;color:#777}.cap{font-weight:600;font-size:13px}
table{border-collapse:collapse;background:#fff;font-size:12.5px;margin:3px 0}
td,th{border:1px solid #999;padding:3px 6px;vertical-align:top}
th{background:#e9eef6;font-weight:600}td.num{text-align:right;white-space:nowrap}
td.ocr{background:#fff6d6}td .mis{color:#b45309;font-size:10.5px;display:block}
td table{width:100%}.note{font-size:12px;color:#555;margin:1px 0}
details{font-size:12px;margin-top:3px}details li{list-style:none;margin:0}.path{color:#555}.val{font-weight:600}
.img{font-size:12px;color:#777;border-left:3px solid #ccc;padding-left:8px;margin:6px 0}
.legend span{display:inline-block;padding:0 6px;margin-right:6px;border:1px solid #999}
"""


def is_num(text):
    stripped = text.replace(",", "").replace("%", "").replace(" ", "").strip("()+-")
    return bool(stripped) and all(ch.isdigit() or ch == "." for ch in stripped)


class Renderer:
    def __init__(self, data):
        self.data = data
        self.tables = {n["id"]: n for n in data["nodes"] if n["type"] == "table"}
        self.sections = {s["id"]: s for s in data["sections"]}

    def table(self, node):
        out = ['<div class="tbl">']
        meta = f'{node["id"]} · p{node["page"]} · {node["kind"]} {node["n_rows"]}x{node["n_cols"]} · 머리글 규칙={node["header_rule"]}'
        if node.get("continued_from"):
            meta += f' · {node["continued_from"]}에서 이어짐'
        if node.get("context_path"):
            meta += " · 위치: " + " / ".join(node["context_path"])
        if node.get("caption"):
            out.append(f'<div class="cap">{escape(node["caption"])}</div>')
        if node.get("unit"):
            meta += f' · 단위: {escape(node["unit"])}'
        out.append(f'<div class="meta">{escape(meta)}</div>')
        out.append(self.grid(node))
        for note in node.get("notes", []):
            out.append(f'<div class="note">{escape(note["text"])}</div>')
        if node["values"]:
            items = "".join(
                f'<li><span class="path">{escape(" / ".join(v["row_path"]) or "·")} ‖ '
                f'{escape(" / ".join(v["col_path"]) or "·")}</span> = <span class="val">{escape(v["text"][:120])}</span></li>'
                for v in node["values"])
            out.append(f'<details><summary>값 {len(node["values"])}개와 머리글 경로</summary><ul>{items}</ul></details>')
        out.append("</div>")
        return "".join(out)

    def grid(self, node):
        rows, taken = {}, set()
        for cell in node["cells"]:
            key = (cell["r"], cell["c"])
            if key in taken:
                continue  # 겹치는 칸은 그릴 수 없으므로 앞의 것만 그린다
            rows.setdefault(cell["r"], []).append(cell)
            taken.update((r, c) for r in range(cell["r"], cell["r"] + cell["rs"])
                         for c in range(cell["c"], cell["c"] + cell["cs"]))
        # 칸이 없는 자리는 빈 칸으로 채워 열이 밀리지 않게 한다
        for r in range(node["n_rows"]):
            for c in range(node["n_cols"]):
                if (r, c) not in taken:
                    rows.setdefault(r, []).append({"r": r, "c": c, "rs": 1, "cs": 1, "text": "", "header": False, "hole": True})
        html = ["<table>"]
        for r in range(node["n_rows"]):
            html.append("<tr>")
            for cell in sorted(rows.get(r, []), key=lambda c: c["c"]):
                tag = "th" if cell["header"] else "td"
                classes = []
                if tag == "td" and is_num(cell["text"]):
                    classes.append("num")
                if cell.get("source") == "ocr":
                    classes.append("ocr")
                span = (f' rowspan="{cell["rs"]}"' if cell["rs"] > 1 else "") + (f' colspan="{cell["cs"]}"' if cell["cs"] > 1 else "")
                cls = f' class="{" ".join(classes)}"' if classes else ""
                body = "<br>".join(escape(line) for line in cell.get("lines") or [cell["text"]]) if cell["text"] else ""
                if cell.get("ocr_match") is False:
                    body += f'<span class="mis">OCR: {escape(cell.get("ocr_text", "")[:60])}</span>'
                for note in cell.get("notes", []) or []:
                    body += f'<div class="note">{escape(note["text"])}</div>'
                for child in cell.get("child_tables") or []:
                    if child in self.tables:
                        body += self.table(self.tables[child])
                html.append(f"<{tag}{span}{cls}>{body}</{tag}>")
            html.append("</tr>")
        html.append("</table>")
        return "".join(html)

    def node(self, node):
        kind = node["type"]
        if kind == "heading":
            section = self.sections.get(node.get("section"), {})
            level = min(section.get("level", 4), 4)
            return f'<h{level} id="{section.get("id", "")}">{escape(node["text"])}</h{level}>'
        if kind == "paragraph":
            cls = ' class="li"' if node.get("list_item") else ""
            return f'<p{cls}>{escape(node["text"])}</p>'
        if kind == "toc_entry":
            target = f' … {node["target_page"]}' if node.get("target_page") else ""
            return f'<div class="toc">{escape(node["text"])}{target}</div>'
        if kind == "table":
            return self.table(node)
        if kind == "image":
            text = " ".join(line["text"] for line in node.get("ocr_lines") or [])
            return f'<div class="img">[이미지] {escape(text[:200]) or "(글자 없음)"}</div>'
        return ""

    def render(self):
        data = self.data
        nav = ['<nav><b>섹션</b>']
        for s in data["sections"]:
            nav.append(f'<a class="l{min(s["level"], 4)}" href="#{s["id"]}">{escape(s["title"][:40])} <small>p{s["page"]}</small></a>')
        nav.append("</nav>")
        doc = data["document"]
        info = " · ".join(f"{k}: {escape(str(v.get('value')))}" for k, v in doc.items() if isinstance(v, dict))
        check = data.get("check", {})
        missing = sum((check.get("missing_chars_by_page") or {}).values())
        counts = {}
        for n in data["nodes"]:
            counts[n["type"]] = counts.get(n["type"], 0) + 1
        body = [f'<main><div class="info"><b>{escape(data["source"].get("file", "") if isinstance(data["source"], dict) else str(data["source"]))}</b><br>'
                f'{info}<br>노드 {counts} · 섹션 {len(data["sections"])} · 빠진 글자 {missing}'
                '<div class="legend meta">표기: <span style="background:#e9eef6">머리글 칸</span>'
                '<span style="background:#fff6d6">OCR로 채운 칸</span><span>주황 글씨 = PDF 글자와 OCR이 다름</span></div></div>']
        page = None
        for node in data["nodes"]:
            if node["type"] == "table" and node.get("parent"):
                continue  # 바깥 칸 안에서 그린다
            if node["page"] != page:
                page = node["page"]
                body.append(f'<div class="page">p{page}</div>')
            body.append(self.node(node))
        body.append("</main>")
        title = escape(doc.get("fund_name", {}).get("value", "구조") if isinstance(doc.get("fund_name"), dict) else "구조")
        return (f'<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>{title}</title>'
                f"<style>{CSS}</style></head><body>{''.join(nav)}{''.join(body)}</body></html>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    output = args.output or args.input.with_suffix(".html")
    output.write_text(Renderer(data).render(), encoding="utf-8")
    print(f"output={output}")


if __name__ == "__main__":
    main()
