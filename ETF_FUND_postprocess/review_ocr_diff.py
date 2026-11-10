"""원본 PDF와 OCR 전용 재구성을 쪽별로 검토하는 오프라인 HTML을 만든다.

사용법: python -I review_ocr_diff.py 원본.pdf.raw 1층.json 기준.structure.json
        OCR.structure.json [--ocr2 다른OCR.structure.json] [-o 출력.html]
공백 제거, 표 IoU 0.6, 경로 유사도 0.8 / 글자 0.9, 제목 0.85를 사용한다.
기존 비교기의 정확/유사 집계를 보존한다. 머리글 칸 비교는 별도로 표시한다.
"""
import argparse
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from html import escape
from pathlib import Path
from urllib.parse import quote


def squash(text):
    return re.sub(r"\s+", "", text or "")


def similarity(a, b):
    return SequenceMatcher(None, squash(a), squash(b)).ratio()


def iou(a, b):
    if not a or not b:
        return 0
    w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    intersection = max(0, w) * max(0, h)
    union = ((a[2] - a[0]) * (a[3] - a[1])
             + (b[2] - b[0]) * (b[3] - b[1]) - intersection)
    return intersection / union if union > 0 else 0


def path_key(value):
    return tuple(squash("/".join(value.get(k) or [])) for k in ("row_path", "col_path"))


def path_text(value):
    if value is None:
        return "—"
    return "행: " + " / ".join(value.get("row_path") or []) + "\n열: " + " / ".join(value.get("col_path") or [])


def coord(value):
    return value.get("r"), value.get("c")


def cell_at(table, value):
    """값의 시작 좌표에 해당하는 실제 병합 칸을 찾는다."""
    r, c = coord(value)
    return next((x for x in table["cells"] if coord(x) == (r, c)), None) or next(
        (x for x in table["cells"] if x["r"] <= r < x["r"] + x.get("rs", 1)
         and x["c"] <= c < x["c"] + x.get("cs", 1)), None)


KINDS = {"exact": "정확 일치", "fuzzy": "유사 (OCR 오인식)", "text": "글자 다름",
         "path": "경로 다름", "missing": "누락", "extra": "OCR에만 있음",
         "table_missing": "누락 · 표", "heading_missing": "제목 누락", "heading_extra": "OCR에만 있음 · 제목"}


class Comparison:
    """기준 값의 집계와 화면 연결 정보를 함께 만들되, 집계에는 값만 넣는다."""
    def __init__(self, ref, ocr, page_numbers):
        self.ref, self.ocr = ref, ocr
        self.pages = {p: {"records": [], "paragraph_similarity": None} for p in page_numbers}
        self.stats = Counter({k: 0 for k in ("values", "exact", "fuzzy", "diff", "missing",
            "tables", "tables_matched", "tables_extra", "sections", "sections_found", "sections_extra")})
        self.cells, self.tables, self.headings = defaultdict(list), defaultdict(list), defaultdict(list)
        self.serial = 0

    def add(self, page, kind, original=None, other=None, bbox=None, table=None, cell=None,
            heading=None, scope="value", location=""):
        record = {"id": f"d{self.serial}", "kind": kind, "label": KINDS[kind], "scope": scope,
                  "original": (original or {}).get("text", ""), "ocr": (other or {}).get("text", ""),
                  "original_path": path_text(original), "ocr_path": path_text(other),
                  "bbox": bbox, "location": location}
        self.serial += 1
        self.pages[page]["records"].append(record)
        if table is not None:
            target = self.cells[(table["id"], *coord(cell))] if cell is not None else self.tables[table["id"]]
            target.append(record)
        if heading:
            self.headings[heading].append(record)
        return record

    def values(self, original, other):
        by_path = defaultdict(list)
        for i, value in enumerate(other["values"]):
            by_path[path_key(value)].append((i, value))
        used, ref_cells, ocr_cells = set(), set(), set()
        for value in original["values"]:
            key, chosen, kind = path_key(value), None, "path"
            if key in by_path:
                candidates = by_path[key]
                chosen = next((v for v in candidates if squash(v[1]["text"]) == squash(value["text"])), candidates[0])
                kind = "exact" if squash(chosen[1]["text"]) == squash(value["text"]) else "text"
            else:
                near = max(by_path, key=lambda k: similarity(k[0], key[0]) + similarity(k[1], key[1]), default=None)
                if near is not None and all(similarity(a, b) >= .8 for a, b in zip(near, key)):
                    candidates = by_path[near]
                    chosen = max(candidates, key=lambda v: similarity(v[1]["text"], value["text"]))
                    if similarity(chosen[1]["text"], value["text"]) >= .9:
                        kind = "fuzzy"
                # 경로도 크게 달라진 칸은 같은 좌표로 연결하되, 일치로 집계하지 않는다.
                if chosen is None:
                    chosen = next(((i, v) for i, v in enumerate(other["values"])
                                   if i not in used and coord(v) == coord(value)), None)
                if chosen is None:
                    kind = "missing"
            self.stats[kind if kind in ("exact", "fuzzy", "missing") else "diff"] += 1
            rc = cell_at(original, value)
            oc = cell_at(other, chosen[1]) if chosen else None
            if rc:
                ref_cells.add(coord(rc))
            if oc:
                ocr_cells.add(coord(oc))
            if chosen:
                used.add(chosen[0])
            self.add(original["page"], kind, value, chosen[1] if chosen else None,
                     (rc or original).get("bbox"), other if chosen else None, oc,
                     location=f'{original["id"]} · 값 ({value["r"] + 1}, {value["c"] + 1})')
        for i, value in enumerate(other["values"]):
            if i not in used:
                oc = cell_at(other, value)
                if oc:
                    ocr_cells.add(coord(oc))
                self.add(other["page"], "extra", other=value, table=other, cell=oc,
                         location=f'{other["id"]} · 추가 값')
        # values[]에 없는 머리글/일반 칸도 검토한다. 값 집계와는 분리한다.
        for rc in original["cells"]:
            if coord(rc) in ref_cells or not squash(rc.get("text")):
                continue
            candidates = [c for c in other["cells"] if coord(c) not in ocr_cells]
            oc = max(candidates, key=lambda c: iou(rc.get("bbox"), c.get("bbox")), default=None)
            if oc is None or iou(rc.get("bbox"), oc.get("bbox")) < .3:
                oc = next((c for c in candidates if coord(c) == coord(rc)), None)
            kind = ("missing" if oc is None else "exact" if squash(rc["text"]) == squash(oc["text"])
                    else "fuzzy" if similarity(rc["text"], oc["text"]) >= .9 else "text")
            if oc:
                ocr_cells.add(coord(oc))
            self.add(original["page"], kind, rc, oc, rc.get("bbox"), other if oc else None, oc,
                     scope="cell", location=f'{original["id"]} · 머리글/일반 칸')
        for oc in other["cells"]:
            if coord(oc) not in ocr_cells and squash(oc.get("text")):
                self.add(other["page"], "extra", other=oc, table=other, cell=oc,
                         scope="cell", location=f'{other["id"]} · 추가 칸')

    def run(self):
        rt = [n for n in self.ref["nodes"] if n["type"] == "table"]
        ot = [n for n in self.ocr["nodes"] if n["type"] == "table"]
        for table in rt:
            self.stats["tables"] += 1
            self.stats["values"] += len(table["values"])
            candidates = [(iou(table.get("bbox"), o.get("bbox")), o) for o in ot if o["page"] == table["page"]]
            score, other = max(candidates, key=lambda x: x[0], default=(0, None))
            if score >= .6:
                self.stats["tables_matched"] += 1
                self.values(table, other)
            else:
                self.add(table["page"], "table_missing", {"text": table.get("caption") or table["id"]},
                         bbox=table.get("bbox"), scope="table", location=table["id"])
                self.stats["missing"] += len(table["values"])
                for value in table["values"]:
                    self.add(table["page"], "missing", value, bbox=(cell_at(table, value) or table).get("bbox"),
                             location=f'{table["id"]} · 표 안의 값')
        for table in ot:
            # 기존 비교기처럼 모든 기준 표와의 IoU로 추가 표를 판단한다.
            if any(t["page"] == table["page"] and iou(t.get("bbox"), table.get("bbox")) >= .6 for t in rt):
                continue
            self.stats["tables_extra"] += 1
            self.add(table["page"], "extra", other={"text": table.get("caption") or table["id"]},
                     table=table, scope="table", location=table["id"])
            for cell in table["cells"]:
                if squash(cell.get("text")):
                    self.add(table["page"], "extra", other=cell, table=table, cell=cell,
                             scope="cell", location=f'{table["id"]} · 추가 표의 칸')
        for section in self.ref["sections"]:
            self.stats["sections"] += 1
            if any(s["page"] == section["page"] and similarity(s["title"], section["title"]) >= .85
                   for s in self.ocr["sections"]):
                self.stats["sections_found"] += 1
                continue
            headings = [n for n in self.ref["nodes"] if n["type"] == "heading" and n["page"] == section["page"]]
            heading = next((n for n in headings if n.get("section") == section["id"]), None)
            if heading is None:
                heading = next((n for n in headings if similarity(n.get("text"), section["title"]) >= .85), {})
            self.add(section["page"], "heading_missing", {"text": section["title"]},
                     bbox=heading.get("bbox"), scope="heading")
        for section in self.ocr["sections"]:
            if not any(s["page"] == section["page"] and similarity(s["title"], section["title"]) >= .85
                       for s in self.ref["sections"]):
                self.stats["sections_extra"] += 1
                self.add(section["page"], "heading_extra", other={"text": section["title"]},
                         heading=section["id"], scope="heading")
        for p, page in self.pages.items():
            texts = ["".join(n.get("text", "") for n in doc["nodes"]
                             if n["page"] == p and n["type"] == "paragraph") for doc in (self.ref, self.ocr)]
            page["paragraph_similarity"] = round(similarity(*texts) * 100, 1) if any(texts) else None
            page["difference_count"] = sum(r["kind"] != "exact" for r in page["records"])
        return self


def tooltip(records):
    return "\n\n".join(f'{r["label"]} · {r["location"]}\n원본: {r["original"] or "—"}\n'
                       f'OCR: {r["ocr"] or "—"}\n원본 {r["original_path"]}\nOCR {r["ocr_path"]}' for r in records)


def attributes(records):
    if not records:
        return ""
    kinds = {r["kind"] for r in records}
    color = ("bad" if kinds & {"text", "path", "missing"} else "fuzzy" if "fuzzy" in kinds
             else "exact" if "exact" in kinds else "extra")
    links = " ".join(r["id"] for r in records)
    return f' class="linked {color}" data-links="{links}" tabindex="0" title="{escape(tooltip(records), quote=True)}"'


class Renderer:
    """render_structure.py와 같은 병합 칸·안쪽 표·빈자리 채움 규칙을 적용한다."""
    def __init__(self, comparison):
        self.cmp = comparison
        self.tables = {n["id"]: n for n in comparison.ocr["nodes"] if n["type"] == "table"}
        self.sections = {s["id"]: s for s in comparison.ocr["sections"]}

    def table(self, node, ancestors=()):
        if node["id"] in ancestors:
            return '<p class="meta">중첩 표 순환 참조</p>'
        ancestors = (*ancestors, node["id"])
        meta = f'{node["id"]} · {node["n_rows"]}행 × {node["n_cols"]}열'
        meta += " · " + " / ".join(node.get("context_path") or [])
        out = [f'<div{attributes(self.cmp.tables[node["id"]])}><div class="tbl">',
               f'<div class="cap">{escape(node.get("caption") or "")}</div>',
               f'<div class="meta">{escape(meta)} {escape(node.get("unit") or "")}</div><table>']
        rows, taken = defaultdict(list), set()
        for cell in node["cells"]:
            if coord(cell) in taken:
                continue
            rows[cell["r"]].append(cell)
            taken.update((r, c) for r in range(cell["r"], cell["r"] + cell.get("rs", 1))
                         for c in range(cell["c"], cell["c"] + cell.get("cs", 1)))
        for r in range(node["n_rows"]):
            for c in range(node["n_cols"]):
                if (r, c) not in taken:
                    rows[r].append({"r": r, "c": c, "text": ""})
            out.append("<tr>")
            for cell in sorted(rows[r], key=lambda c: c["c"]):
                tag = "th" if cell.get("header") else "td"
                attrs = attributes(self.cmp.cells[(node["id"], *coord(cell))])
                body = "<br>".join(escape(str(line)) for line in (cell.get("lines") or [cell.get("text", "")]))
                for child in cell.get("child_tables") or []:
                    if child in self.tables:
                        body += self.table(self.tables[child], ancestors)
                for note in cell.get("notes") or []:
                    body += f'<div class="note">{escape(note.get("text", "") if isinstance(note, dict) else str(note))}</div>'
                out.append(f'<{tag}{attrs} rowspan="{cell.get("rs", 1)}" colspan="{cell.get("cs", 1)}">{body}</{tag}>')
            out.append("</tr>")
        out.append("</table>")
        for note in node.get("notes") or []:
            out.append(f'<div class="note">{escape(note.get("text", "") if isinstance(note, dict) else str(note))}</div>')
        out.append("</div></div>")
        return "".join(out)

    def pages(self):
        by_page = defaultdict(list)
        for node in self.cmp.ocr["nodes"]:
            if node["type"] != "table" or not node.get("parent"):
                by_page[node["page"]].append(node)
        result = {}
        for p in self.cmp.pages:
            out = []
            for node in sorted(by_page[p], key=lambda n: ((n.get("bbox") or [0, 0])[1], (n.get("bbox") or [0])[0])):
                kind = node["type"]
                if kind == "table":
                    out.append(self.table(node))
                elif kind == "heading":
                    level = max(1, min(self.sections.get(node.get("section"), {}).get("level", 4), 4))
                    out.append(f'<h{level}{attributes(self.cmp.headings[node.get("section")])}>{escape(node.get("text", ""))}</h{level}>')
                elif kind in ("paragraph", "toc_entry"):
                    cls = "toc" if kind == "toc_entry" else "paragraph"
                    target = f' … {node["target_page"]}' if kind == "toc_entry" and node.get("target_page") else ""
                    out.append(f'<p class="{cls}">{escape(node.get("text", ""))}{escape(target)}</p>')
            result[p] = "".join(out) or '<p class="meta">이 쪽에 OCR 재구성 내용이 없습니다.</p>'
        return result


CSS = """
*{box-sizing:border-box}body{margin:0;background:#edf0f5;color:#202c40;font:14px/1.55 'Malgun Gothic',sans-serif}
button,input,select{font:inherit}button,select,input{border:1px solid #c2cbd8;border-radius:5px;background:white;padding:5px 9px}
button,select{cursor:pointer}button:hover{background:#e7eefb}button:disabled{opacity:.4;cursor:default}
header{background:#182940;color:white;padding:13px 22px}header h1{font-size:18px;margin:0 0 8px;font-weight:650}
.toolbar{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.toolbar select{max-width:350px}input[type=number]{width:74px}
#summary{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}.stat{padding:3px 10px;background:#2a3e58;border-radius:4px;font-size:12px}
.legend{padding:9px 22px;background:white;display:flex;flex-wrap:wrap;gap:15px;border-bottom:1px solid #d5dce6;font-size:12px}
.legend span:before{content:'';display:inline-block;width:10px;height:10px;margin-right:5px;border:2px solid var(--c);background:var(--bg,transparent)}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px;padding:12px 18px 0}
.pane{min-width:0;border:1px solid #cdd5e0;border-radius:7px;background:white;overflow:hidden}
.pane-title{padding:9px 13px;background:#f7f9fc;border-bottom:1px solid #d8dfeb;display:flex;gap:15px;align-items:center;justify-content:space-between}
.pane-title h2{font-size:14px;margin:0}.pane-title label{font-size:12px;cursor:pointer}.scroll{height:calc(100vh - 355px);min-height:320px;overflow:auto;scroll-behavior:smooth}
#leftScroll{background:#e5e9ef}.sheet{position:relative;width:100%}.sheet img{display:block;width:100%;height:auto}
#overlay{position:absolute;inset:0;pointer-events:none}.box{position:absolute;border:2px solid #d93c48;background:#f34b5b22;pointer-events:auto;cursor:pointer;padding:0;border-radius:1px;min-width:3px;min-height:3px}
.box.fuzzy{border-color:#c78c06;background:#ffdf4b33}.box.table_missing{border-color:#2477e8;background:transparent;pointer-events:none}
.box.table_missing:before{content:'';position:absolute;inset:-3px;border:4px solid transparent;pointer-events:auto}
.box.heading_missing{border-color:#9642c7;background:#c268e82b}.box.exact{opacity:0;pointer-events:none}
.box.selected{opacity:1;pointer-events:auto;outline:3px solid #142c50;outline-offset:2px;background:#83b8ff40;z-index:5}
iframe{width:100%;height:100%;border:0;background:white}#rightContent{padding:12px 18px 40px;overflow-wrap:anywhere}
#rightContent h1{font-size:20px}#rightContent h2{font-size:18px}#rightContent h3{font-size:16px}#rightContent h4{font-size:14px}
#rightContent p{margin:6px 0;text-align:justify}.toc,.meta,.note{font-size:11px;color:#657287}.toc{padding-left:9px}
.tbl{margin:12px 0 20px}.cap{font-weight:650;font-size:13px}table{border-collapse:collapse;width:100%;margin-top:5px;font-size:12px;color:#273347}
td,th{border:1px solid #a6b1c0;padding:4px 6px;vertical-align:top;min-width:16px}th{background:#edf1f8}td table{font-size:11px}
.linked{cursor:pointer}.linked.exact{background:#e3f3e9}.linked.fuzzy{background:#fff1b8}.linked.bad{background:#ffe0e1}
.linked.extra{outline:2px dashed #88909c;outline-offset:-2px}.linked.selected{outline:3px solid #1d5abb;outline-offset:-2px;box-shadow:0 0 0 3px #a8c8ff}
.linked:focus-visible,button:focus-visible{outline:3px solid #1d5abb}#diffPanel{margin:12px 18px;background:white;border:1px solid #cdd5e0;border-radius:6px}
#diffPanel summary{padding:9px 13px;cursor:pointer;font-weight:600}#differences{max-height:210px;overflow:auto;padding:0 10px 10px}
.difference{display:block;text-align:left;width:100%;margin:5px 0;padding:8px 10px;border-left:4px solid #da5963;font-size:12px;white-space:normal;overflow-wrap:anywhere}
.difference.fuzzy{border-left-color:#d6a620}.difference.heading_missing{border-left-color:#9642c7}.difference.extra,.difference.heading_extra{border-left-color:#88909c}
.difference.table_missing{border-left-color:#2477e8}.difference.selected{background:#e8f0ff;outline:2px solid #1d5abb}.difference small{display:block;color:#586578;white-space:pre-wrap}
#pageInfo{font-size:12px;color:#586578;padding:8px 22px 0}#status{font-size:12px;padding:0 22px 12px;color:#47566d;min-height:26px}
[hidden]{display:none!important}@media(max-width:850px){.workspace{grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:6px;padding:6px}.scroll{height:65vh}.toolbar select{max-width:220px}}
"""


JS = r"""
'use strict';
const DATA=JSON.parse(document.getElementById('review-data').textContent);
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let sourceIndex=0, pageIndex=0, currentRecords=new Map();
const pages=DATA.pages, source=()=>DATA.sources[sourceIndex], page=()=>pages[pageIndex];
function summary(){
  const s=source().summary;
  $('summary').innerHTML=[`섹션 찾음 ${s.sections_found}/${s.sections} · 추가 ${s.sections_extra}`,
    `표 찾음 ${s.tables_matched}/${s.tables} · 추가 ${s.tables_extra}`,`기준 값 ${s.values}`,
    `정확 ${s.exact}`,`유사 ${s.fuzzy}`,`다름 ${s.diff}`,`누락 ${s.missing}`].map(t=>`<span class="stat">${esc(t)}</span>`).join('');
  $('rank').innerHTML='<option value="">차이 많은 쪽</option>'+pages.slice().sort((a,b)=>
    source().pages[b.page].difference_count-source().pages[a.page].difference_count||a.page-b.page).map(p=>
      `<option value="${p.page}">${p.page}쪽 · ${source().pages[p.page].difference_count}건</option>`).join('');
}
function showPage(){
  const p=page(), d=source().pages[p.page]; currentRecords=new Map(d.records.map(r=>[r.id,r]));
  $('page').value=p.page; $('page').max=pages.at(-1).page; $('totalPages').textContent=`/ ${pages.at(-1).page}쪽`;
  $('prev').disabled=pageIndex===0; $('next').disabled=pageIndex===pages.length-1;
  $('leftTitle').textContent=`원본 · ${p.page}쪽`; $('rightTitle').textContent=`OCR 재구성 · ${p.page}쪽`;
  $('rightSource').textContent=source().label; $('rightContent').innerHTML=d.html;
  $('pageImage').src=p.image; $('sheet').style.aspectRatio=`${p.width} / ${p.height}`;
  $('overlay').replaceChildren();
  const area=r=>r.bbox?(r.bbox[2]-r.bbox[0])*(r.bbox[3]-r.bbox[1]):0;
  for(const r of d.records.slice().sort((a,b)=>area(b)-area(a))){
    if(!r.bbox) continue;
    const [x0,y0,x1,y1]=r.bbox, box=document.createElement('button');
    box.className=`box ${r.kind}`; box.dataset.links=r.id; box.title=tip(r); box.setAttribute('aria-label',tip(r));
    box.style.cssText=`left:${100*x0/p.width}%;top:${100*y0/p.height}%;width:${100*(x1-x0)/p.width}%;height:${100*(y1-y0)/p.height}%`;
    if(r.kind==='exact') box.tabIndex=-1;
    $('overlay').append(box);
  }
  const diffs=d.records.filter(r=>r.kind!=='exact');
  $('diffTitle').textContent=`차이 목록 · ${diffs.length}건 (값·칸·표·제목)`;
  $('differences').innerHTML=diffs.map(r=>`<button class="difference ${r.kind}" data-links="${r.id}" title="${esc(tip(r))}">
    <b>${esc(r.label)}</b> · ${esc(r.location)}${r.scope==='cell'?' · 값 집계 제외':''}
    <small>원본: ${esc(r.original||'—')} → OCR: ${esc(r.ocr||'—')}</small>
    <small>원본 ${esc(r.original_path)}\nOCR ${esc(r.ocr_path)}</small></button>`).join('')||'<p>이 쪽에는 차이가 없습니다.</p>';
  $('pageInfo').textContent=`${p.page}쪽 · 차이 ${diffs.length}건 · 문단 글자 유사도 ${d.paragraph_similarity===null?'비교할 문단 없음':d.paragraph_similarity+'%'} · 공백 제외 · 상단 값 집계는 머리글/일반 칸 제외`;
  $('status').textContent='칸·상자·목록을 클릭하면 대응 위치를 선택합니다. 초록 칸도 클릭하여 원본 위치를 볼 수 있습니다.';
  $('leftScroll').scrollTop=0; $('rightContent').scrollTop=0;
  pdfMode(); history.replaceState(null,'',`#page=${p.page}`);
}
function tip(r){return `${r.label} · ${r.location}\nOCR: ${r.ocr||'—'}\n원본: ${r.original||'—'}\n원본 ${r.original_path}\nOCR ${r.ocr_path}`;}
function pdfMode(){
  const on=$('pdfMode').checked; $('sheet').hidden=on; $('pdfFrame').hidden=!on;
  if(on) $('pdfFrame').src=DATA.pdf+`#page=${page().page}`;
  else $('pdfFrame').removeAttribute('src');
}
function navigate(number){
  const index=pages.findIndex(p=>p.page===Number(number));
  if(index<0){$('page').value=page().page;return;}
  pageIndex=index; showPage();
}
function choose(ids, origin){
  const records=ids.split(' ').map(id=>currentRecords.get(id)).filter(Boolean); if(!records.length)return;
  document.querySelectorAll('.selected').forEach(el=>el.classList.remove('selected'));
  const targets=[...document.querySelectorAll('[data-links]')].filter(el=>el.dataset.links.split(' ').some(id=>ids.split(' ').includes(id)));
  targets.forEach(el=>el.classList.add('selected'));
  const box=targets.find(el=>el.classList.contains('box'));
  if(box){$('pdfMode').checked=false;pdfMode();box.scrollIntoView({block:'center',inline:'nearest'});}
  const right=targets.find(el=>$('rightContent').contains(el));
  if(right && !$('rightContent').contains(origin)) right.scrollIntoView({block:'center',inline:'nearest'});
  const item=targets.find(el=>el.classList.contains('difference'));
  if(item && $('diffPanel').open && !$('differences').contains(origin)) item.scrollIntoView({block:'nearest'});
  const r=records[0]; $('status').textContent=`${r.label} · ${r.location} · ${box?'원본 위치 선택됨':'대응 원본 상자 없음'}${right?'':' · 대응 OCR 칸 없음'}`;
}
$('source').innerHTML=DATA.sources.map((s,i)=>`<option value="${i}">${esc(s.label)}</option>`).join('');
$('source').disabled=DATA.sources.length===1;
$('source').onchange=()=>{sourceIndex=Number($('source').value);summary();showPage();};
$('prev').onclick=()=>{if(pageIndex>0)navigate(pages[pageIndex-1].page);};
$('next').onclick=()=>{if(pageIndex<pages.length-1)navigate(pages[pageIndex+1].page);};
$('page').onchange=()=>navigate($('page').value); $('rank').onchange=()=>{if($('rank').value)navigate($('rank').value);};
$('pdfMode').onchange=pdfMode;
document.addEventListener('click',e=>{const el=e.target.closest('[data-links]');if(el)choose(el.dataset.links,el);});
document.addEventListener('keydown',e=>{
  if(['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)||e.target.isContentEditable)return;
  if((e.key==='Enter'||e.key===' ') && e.target.matches('.linked')){e.preventDefault();choose(e.target.dataset.links,e.target);}
  if(e.key==='ArrowLeft'){e.preventDefault();$('prev').click();}
  if(e.key==='ArrowRight'){e.preventDefault();$('next').click();}
});
const initial=Number(new URLSearchParams(location.hash.slice(1)).get('page'));
if(pages.some(p=>p.page===initial))pageIndex=pages.findIndex(p=>p.page===initial);
summary(); showPage();
"""


def relative_url(path, output):
    return quote(Path(os.path.relpath(path, output.parent)).as_posix(), safe="/")


def build(args):
    read = lambda p: json.loads(p.read_text(encoding="utf-8-sig"))
    layer, ref = read(args.layer1), read(args.reference)
    output = (args.output or args.layer1.with_suffix(".ocr_review.html")).resolve()
    pdf_copy = output.parent / (args.layer1.stem + ".review.pdf.raw")
    inputs = [args.pdf, args.layer1, args.reference, args.ocr, *([args.ocr2] if args.ocr2 else [])]
    if output in [p.resolve() for p in inputs] or pdf_copy in [p.resolve() for p in inputs]:
        raise ValueError("출력 경로가 입력 파일과 겹칩니다. 다른 출력 이름을 지정하세요.")
    if output == pdf_copy:
        raise ValueError("HTML과 PDF 복사본의 경로가 겹칩니다.")
    with args.pdf.open("rb") as pdf:
        if pdf.read(5) != b"%PDF-":
            raise ValueError("원본 파일이 PDF 형식이 아닙니다.")
    pages = []
    for page in sorted(layer["pages"], key=lambda p: p["page"]):
        image = (args.layer1.parent / page["file"]).resolve()
        if not image.is_file():
            raise ValueError(f"페이지 이미지가 없습니다: {image}")
        if page["width"] <= 0 or page["height"] <= 0:
            raise ValueError(f'페이지 크기가 잘못되었습니다: {page["page"]}')
        pages.append({k: page[k] for k in ("page", "width", "height")})
        pages[-1]["image"] = relative_url(image, output)
    numbers = [p["page"] for p in pages]
    if not numbers or len(set(numbers)) != len(numbers):
        raise ValueError("페이지가 없거나 페이지 번호가 중복됩니다.")
    sources = []
    for path in [args.ocr, *([args.ocr2] if args.ocr2 else [])]:
        ocr = read(path)
        unknown = {n["page"] for doc in (ref, ocr) for n in doc["nodes"] + doc["sections"]} - set(numbers)
        if unknown:
            raise ValueError(f"1층에 없는 페이지 번호: {sorted(unknown)}")
        comparison = Comparison(ref, ocr, numbers).run()
        html_pages = Renderer(comparison).pages()
        for p, content in html_pages.items():
            comparison.pages[p]["html"] = content
        label = "OCR · " + path.name
        sources.append({"label": label, "summary": dict(comparison.stats), "pages": comparison.pages})
        print(f"{path.name}: {json.dumps(dict(comparison.stats), ensure_ascii=False)}")
    fund = ref.get("document", {}).get("fund_name", {})
    title = fund.get("value") if isinstance(fund, dict) else fund
    title = title or args.layer1.stem
    data = {"document": title, "pages": pages, "pdf": relative_url(pdf_copy, output), "sources": sources}
    # script 종료 문자열 및 HTML 해석을 막는다. 사용자 글자는 textContent/escape로 출력한다.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    body = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(str(title))} · OCR 검토</title>
<style>{CSS}</style></head><body><header><h1>{escape(str(title))} · OCR 비교 검토</h1>
<div class="toolbar"><label>OCR 출처 <select id="source" aria-label="OCR 출처"></select></label>
<button id="prev" aria-label="이전 쪽">← 이전</button><label>쪽 <input id="page" type="number" min="1" aria-label="쪽 번호"></label>
<span id="totalPages"></span><button id="next" aria-label="다음 쪽">다음 →</button>
<select id="rank" aria-label="차이 많은 쪽"></select><span style="font-size:12px">← → 키로 이동</span></div><div id="summary"></div></header>
<div class="legend"><span style="--c:#429667;--bg:#e3f3e9">정확 일치</span><span style="--c:#c78c06;--bg:#fff1b8">유사</span>
<span style="--c:#d93c48;--bg:#ffe0e1">다름 / 누락</span><span style="--c:#2477e8">원본에만 있는 표</span>
<span style="--c:#9642c7">제목 누락</span><span style="--c:#88909c">OCR에만 있음</span></div><div id="pageInfo"></div>
<main class="workspace"><section class="pane"><div class="pane-title"><h2 id="leftTitle">원본</h2>
<label><input id="pdfMode" type="checkbox"> 원본 PDF로 보기</label></div><div id="leftScroll" class="scroll">
<div id="sheet" class="sheet"><img id="pageImage" alt="원본 PDF 페이지"><div id="overlay"></div></div>
<iframe id="pdfFrame" title="원본 PDF" hidden></iframe></div></section>
<section class="pane"><div class="pane-title"><h2 id="rightTitle">OCR 재구성</h2><span id="rightSource" class="meta"></span></div>
<div id="rightContent" class="scroll"></div></section></main>
<details id="diffPanel"><summary id="diffTitle">차이 목록</summary><div id="differences"></div></details><div id="status" role="status"></div>
<script id="review-data" type="application/json">{payload}</script><script>{JS}</script></body></html>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.pdf, pdf_copy)
    output.write_text(body, encoding="utf-8")
    print(f"HTML: {output}\nPDF: {pdf_copy}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("layer1", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("ocr", type=Path)
    parser.add_argument("--ocr2", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    try:
        build(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"생성 실패: {exc}\n")


if __name__ == "__main__":
    main()
