"""Extract conservative, source-linked ETF values from OCR structures (stdlib only).

python extract_fields.py output/<stem>/<stem>.ocr_structure.json
python extract_fields.py --all

All data leaves are {value, page, node, raw}, or null. Percentages remain in
percent units. Financial/history amounts retain their values and add value_krw.
CSV flattens list
indices and includes provenance columns; coverage groups list leaves with ';'.
"""
import argparse
import calendar
import csv
import datetime as dt
import json
import re
import sys
import unicodedata
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "output"
HORIZONS = ("1y", "2y", "3y", "5y", "since_inception")
FEE_KEYS = ("management", "ap", "trustee", "admin", "total", "other_expenses",
            "ter", "peer_total", "synthetic_ter")
DATE_RE = re.compile(r"(?<!\d)(\d{4}|\d{2})\s*[./년-]\s*(\d{1,2})\s*[./월-]\s*(\d{1,2})(?:일)?(?!\d)")


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def number(raw):
    """Repair OCR letters only inside an otherwise entirely numeric cell."""
    s = compact(raw).replace(",", "").replace("−", "-").replace("–", "-")
    s = s.rstrip("%")
    if s in ("", "-", "—", "－"):
        return None
    if not re.fullmatch(r"[-+()0-9OoIl.]+", s):
        return None
    s = s.translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"}))
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", s):
        return None
    return float(s) if "." in s else int(s)


def dates(text):
    found = []
    for m in DATE_RE.finditer(str(text)):
        y, month, day = map(int, m.groups())
        if y < 100:
            y += 2000 if y < 70 else 1900
        try:
            found.append(dt.date(y, month, day).isoformat())
        except ValueError:
            pass
    return found


def evidence(value, node, raw):
    if value is None:
        return None
    return {"value": value, "page": node.get("page"), "node": node.get("id"), "raw": str(raw)}


def value(item):
    return item.get("value") if isinstance(item, dict) else None


def amount(raw, node, unit):
    """Normalize money fields, including disclosed scales with an omitted 원."""
    item = evidence(number(raw), node, raw)
    if item is None:
        return None
    scales = {"": 1, "십": 10, "백": 100, "천": 1000, "만": 10_000,
              "십만": 100_000, "백만": 1_000_000, "천만": 10_000_000,
              "억": 100_000_000, "십억": 1_000_000_000, "백억": 10_000_000_000,
              "천억": 100_000_000_000, "조": 1_000_000_000_000}
    tokens = re.split(r"[,/·]", compact(value(unit)))
    multipliers = {scales[t[:-1]] for t in tokens if t.endswith("원") and t[:-1] in scales}
    if not multipliers:
        multipliers = {scales[t] for t in tokens if t and t in scales}
    krw = Decimal(str(item["value"])) * next(iter(multipliers)) if len(multipliers) == 1 else None
    item["value_krw"] = (int(krw) if krw == krw.to_integral_value() else float(krw)) if krw is not None else None
    return item


def horizon(text):
    s = compact(text)
    if "년차" in s:
        return None
    if "설정일이후" in s or "설정이후" in s:
        return "since_inception"
    m = re.search(r"(?:최근)?([1235])년", s)
    return m[1] + "y" if m else None


def fee_key(text):
    s = re.sub(r"[·ㆍᆞ•∙・.,()\[\]①-⑳]", "", compact(text))
    if "합성" in s or ("피투자" in s and "보수" in s):
        return "synthetic_ter"
    if "동종" in s and "보수" in s:
        return "peer_total"
    if "집합투자업자" in s or "운용보수" in s:
        return "management"
    if "지정참가" in s or "판매회사보수" in s:
        return "ap"
    if any(t in s for t in ("신탁업자", "수탁보수", "수탁회사", "신탁회사")):
        return "trustee"
    if "일반사무" in s or "사무관리보수" in s:
        return "admin"
    if "기타비용" in s:
        return "other_expenses"
    if "총보수비용" in s:
        return "ter"
    if "총보수" in s or "보수합계" in s:
        return "total"
    return None


# 아래 보조 규칙은 기존 규칙이 끝난 뒤 빈 칸에만 쓴다 (일반 펀드 투자설명서 대응).
FEE_ROLES = (("management", r"집합투자업자|집합투자"), ("ap", r"판매회사|지정참가"),
             ("trustee", r"신탁업자|수탁회사"), ("admin", r"일반사무|사무관리"))


def fee_key_loose(text):
    """Header words without '보수' or with common OCR typos (판매 회사, 신탈 보수, 사문 보수)."""
    s = re.sub(r"[·ㆍᆞ•∙・.,()\[\]①-⑳]", "", compact(text))
    if fee_key(s) or "수수료" in s:
        return None
    if re.fullmatch(r"(?:집합투자업자|운용)(?:보수)?", s):
        return "management"
    if re.fullmatch(r"판매(?:회사)?(?:보수)?", s) and s != "판매":
        return "ap"
    if re.fullmatch(r"(?:신탁업자|신탁|신탈|수탁)(?:보수)?", s) and s not in ("신탁", "수탁"):
        return "trustee"
    if re.fullmatch(r"(?:일반)?(?:사무관리|사문|사무)(?:회사)?(?:보수)?", s) and s not in ("사무", "일반사무"):
        return "admin"
    return None


def fee_roles(text):
    """Fee roles named in one header cell, in reading order."""
    s = compact(text)
    found = sorted((m.start(), key) for key, pattern in FEE_ROLES for m in [re.search(pattern, s)] if m)
    return list(dict.fromkeys(key for _, key in found))


CLASS_CODE = r"[A-Za-z][A-Za-z0-9]{0,3}(?:\s*-\s*[A-Za-z0-9]{1,4}){0,2}"


def norm_class(code):
    return re.sub(r"[\s\-]", "", code or "").upper() or None


def class_code(label):
    """Share-class code in a row label (예: '수수료미징구-온라인슈퍼(S-P)', 'S-P 수수료…', '종류A …').

    여러 클래스가 한 칸에 합쳐져 있으면 '?', 클래스 행이 아니면 None.
    """
    s = (label or "").strip()
    s = re.sub(r"^투자비용\s*", "", s)
    codes = []
    if "수수료" in s:
        codes = [m[1] for m in re.finditer(r"\(\s*(" + CLASS_CODE + r")\s*[()]", s)]
        # 표 제목 형식: '…[채권]_S-P(수수료미징구-온라인슈퍼-개인연금)'
        codes += [m[1] for m in re.finditer(r"_\s*(" + CLASS_CODE + r")\s*\(", s)]
    if not codes:
        m = re.match(r"(?:종류|클래스|class)?\s*(" + CLASS_CODE + r")(?:형|클래스)?(?=\s|$|\(\s*수수료)", s, re.I)
        if m and (len(s) <= 6 or "수수료" in s or re.match(r"종류|클래스|class", s, re.I)):
            codes = [m[1]]
    codes = list(dict.fromkeys(re.sub(r"\s+", "", c) for c in codes))
    if not codes:
        return None
    return codes[0] if len({norm_class(c) for c in codes}) == 1 else "?"


class Document:
    def __init__(self, data):
        self.data = data
        self.nodes = data.get("nodes", [])
        self.by_id = {n["id"]: n for n in self.nodes}
        self.sections = {s["id"]: s for s in data.get("sections", [])}
        self.tables = [n for n in self.nodes if n.get("type") == "table"]
        self.texts = []
        for n in self.nodes:
            if n.get("type") == "table":
                self.texts.extend((n, c.get("text", "")) for c in n.get("cells", []))
            elif n.get("type") != "toc_entry":
                self.texts.append((n, n.get("text", "")))

    def context(self, n):
        section = self.sections.get(n.get("section"), {})
        return compact(" ".join(section.get("path", [])) + " " +
                       " ".join(n.get("context_path", [])) + " " + str(n.get("caption") or ""))

    def nearby(self, n):
        return " ".join(t for p, t in self.texts if p.get("type") != "table"
                        and p.get("page") == n.get("page") and
                        p.get("bbox", [0, 0])[1] <= n.get("bbox", [0, 10000])[1])

    def entries(self, n):
        """Use explicit paths, then actual grid headers; don't count merged repeats."""
        g = n.get("grid", [])
        cells = n.get("cells", [])
        vals = {(c.get("r"), c.get("c")): c for c in n.get("values", [])}
        first_data = next((r for r, row in enumerate(g) if
                           sum(number(x) is not None for x in row[1:]) >= 2), len(g))
        for c in cells:
            raw = c.get("text", "")
            if number(raw) is None:
                continue
            r, col = c["r"], c["c"]
            v = vals.get((r, col), c)
            cp = v.get("col_path") or c.get("col_path") or []
            rp = v.get("row_path") or c.get("row_path") or []
            # Grid headers supplement incomplete OCR semantic paths.
            heads = [row[col] for row in g[:min(first_data, r)] if col < len(row)]
            cp = list(dict.fromkeys(cp or heads))
            # Explicit continuations retain the original column labels AND
            # period row, even if the new table has no headers of its own.
            for prior in self.continuation_headers(n):
                for row in prior.get("grid", []):
                    if any(number(x) is not None for x in row[1:]):
                        break
                    if col < len(row) and row[col] and row[col] not in cp:
                        cp.append(row[col])
            if not any(cp):
                previous = self.previous(n, same_columns=True)
                if previous:
                    pg = previous.get("grid", [])
                    for row in pg:
                        if col < len(row) and dates(row[col]):
                            cp.append(row[col])
                            break
            if not rp and r < len(g):
                rp = list(dict.fromkeys(x for x in g[r][:col] if x and number(x) is None))
            yield c, " ".join(cp), " ".join(rp)

    def continuation_headers(self, n):
        seen = {n["id"]}
        prior = self.by_id.get(n.get("continued_from"))
        while prior and prior["id"] not in seen and prior.get("n_cols") == n.get("n_cols"):
            seen.add(prior["id"])
            yield prior
            prior = self.by_id.get(prior.get("continued_from"))

    def previous(self, n, same_columns=False):
        for prior in reversed(self.tables[:self.tables.index(n)]):
            if n.get("page", 0) - prior.get("page", 0) > 1:
                break
            if (n.get("section") is not None and prior.get("section") == n.get("section")
                    and (not same_columns or prior.get("n_cols") == n.get("n_cols"))):
                return prior
        return None

    def header_raw(self, n, c, fallback):
        for table in (n, *self.continuation_headers(n), self.previous(n, same_columns=True)):
            if table:
                matches = [h for h in table.get("cells", [])
                           if h["c"] <= c["c"] < h["c"] + h.get("cs", 1)
                           and (table is not n or h["r"] < c["r"])
                           and dates(h.get("text", ""))]
                if matches:
                    return table, matches[-1]["text"]
        return n, fallback

    def returns_date(self, n):
        """Only an explicit date in the caption or immediate title is usable."""
        sources = [(n, n.get("caption") or "")]
        above = [(p, t) for p, t in self.texts if p.get("type") != "table"
                 and p.get("page") == n.get("page") and p.get("bbox") and n.get("bbox")
                 and 0 <= n["bbox"][1] - p["bbox"][3] < 40]
        if above:
            sources.append(max(above, key=lambda item: item[0]["bbox"][3]))
        for node, raw in sources:
            m = re.search(r"(?<![가-힣])기준일\s*[:：]\s*(.*)", raw)
            ds = dates(m[1]) if m else []
            if ds:
                return evidence(ds[0], node, raw)
        return None

    def unit(self, n, money=False):
        seen = set()
        while n and n["id"] not in seen:
            seen.add(n["id"])
            if n.get("unit"):
                raw = n.get("unit_raw") or n["unit"]
                cleaned = re.sub(r"^[\[(<]?\s*단\s*위\s*[:：]\s*", "", n["unit"])
                cleaned = compact(cleaned).strip("[]()<>")
                currencies = {t for t in re.split(r"[,/·]", cleaned) if re.fullmatch(r"[십백천만억조]*원", t)}
                if len(currencies) == 1:
                    cleaned = next(iter(currencies))
                return evidence(cleaned, n, raw)
            n = self.by_id.get(n.get("continued_from"))
        return None

    def metadata(self, key):
        item = self.data.get("document", {}).get(key)
        if item is None:
            return None
        raw = item.get("value") if isinstance(item, dict) else item
        if raw is None:
            return None
        page = item.get("page") if isinstance(item, dict) else None
        choices = [(n, t) for n, t in self.texts if compact(raw) in compact(t)
                   and (page is None or n.get("page") == page)]
        if choices:
            n, text = min(choices, key=lambda nt: len(nt[1]))
            return evidence(raw, n, text)
        # Document metadata is itself an OCR-structure source, not a made-up node.
        return {"value": raw, "page": page, "node": "document." + key, "raw": str(raw)}


def row_kind(label):
    s = compact(label)
    if "변동성" in s:
        return "volatility"
    if "비교지수" in s or "기초지수" in s:
        return "benchmark_returns"
    if any(t in s for t in ("투자신탁", "집합투자", "증권", "종류", "펀드", "클래스")) or re.search(r"class", s, re.I):
        return "returns"
    return None


def table_class(doc, n):
    """Class named in the title right above a table (클래스별로 성과표를 따로 싣는 문서)."""
    above = [(p, t) for p, t in doc.texts if p.get("type") != "table" and p.get("page") == n.get("page")
             and p.get("bbox") and n.get("bbox") and 0 <= n["bbox"][1] - p["bbox"][3] < 60]
    if not above:
        return None
    code = class_code(max(above, key=lambda item: item[0]["bbox"][3])[1])
    return None if code == "?" else code


def class_kind(kind, rp, target, has_target, table_code=None):
    """Apply share-class rules after row_kind; ETF rows (no class code) are unchanged.

    - 받은 클래스가 있으면 다른 클래스의 펀드 행은 버리고, 표에 그 클래스 행이 있으면
      클래스 표시가 없는 펀드 행도 버린다 (비교지수·변동성 행은 그대로).
    - row_kind가 못 알아본 클래스 행(예: 'C(수수료미징구-오프라인)')은 펀드 행으로 본다.
    """
    code = class_code(rp) or (table_code if kind == "returns" else None)
    if kind not in (None, "returns"):
        return kind
    if code:
        if code == "?" or (target and norm_class(code) != target):
            return None
        return "returns"
    if kind == "returns" and target:
        return None  # 클래스 표시가 없는 펀드 행은 받은 클래스 값인지 알 수 없다.
    return kind


def performance(doc, out, target=None):
    candidates = []
    for n in doc.tables:
        text = compact(" ".join(c.get("text", "") for c in n.get("cells", [])))
        ctx = doc.context(n)
        if "투자비용" in ctx or "보수" in ctx:
            continue
        if not ("최근" in text or "설정일이후" in text or "수익률" in ctx):
            continue
        if "운용전문" in ctx or "생년" in text:
            continue
        if target and not any(x in text for x in ("비교지수", "수익률변동성", "투자신탁")):
            # 클래스 행이 많으면 비교지수·투자신탁 행은 다음 쪽 이어진 표로 밀려난다.
            text += compact(" ".join(c.get("text", "") for t in doc.tables if t.get("continued_from") == n["id"]
                                     for c in t.get("cells", [])))
        if not any(x in text for x in ("비교지수", "수익률변동성", "투자신탁")):
            continue
        es = list(doc.entries(n))
        if not any(horizon(cp) for _, cp, _ in es):
            continue
        ds = dates(" ".join(cp for _, cp, _ in es))
        title_date = doc.returns_date(n)
        if not ds and title_date:
            ds = [value(title_date)]
        candidates.append((max(ds, default=""), -n.get("page", 0), n, es))
    if target:
        # 받은 클래스 행은 다음 쪽으로 이어진 표에 있는 경우가 많다. 앞 표와 같은 날짜로 본다.
        picked = {x[2]["id"]: x for x in candidates}
        for n in doc.tables:
            prior = picked.get(n.get("continued_from"))
            if prior and n["id"] not in picked:
                es = list(doc.entries(n))
                if any(horizon(cp) for _, cp, _ in es):
                    picked[n["id"]] = (prior[0], -n.get("page", 0), n, es)
                    candidates.append(picked[n["id"]])
    # Date headers can occur in an undated summary even when the detailed
    # performance table is newer. Inception is independent of that selection.
    for n in doc.tables:
        for c in n.get("cells", []):
            if compact(c.get("text")) not in ("최초설정일", "설정일자"):
                continue
            col = c["c"]
            for row in n.get("grid", [])[c["r"] + 1:]:
                if col < len(row) and dates(row[col]) and out["dates"]["inception_date"] is None:
                    out["dates"]["inception_date"] = evidence(dates(row[col])[0], n, row[col])
                    break
    latest_date = max((x[0] for x in candidates), default="")
    for disclosed, _, n, es in sorted(candidates, key=lambda x: x[:2], reverse=True):
        if disclosed != latest_date:
            continue
        # A single merged row label can cover both fund and benchmark rows.
        fund_rows = sorted({c["r"] for c, cp, rp in es if horizon(cp)
                            and "투자신탁" in compact(rp)})
        tcode = table_class(doc, n) if target else None
        has_target = bool(target) and (norm_class(tcode) == target or
                                       any(norm_class(class_code(rp)) == target for _, _, rp in es))
        for c, cp, rp in es:
            h = horizon(cp)
            if not h:
                continue
            kind = row_kind(rp)
            if "비교지수" in compact(rp) and "투자신탁" in compact(rp) and len(fund_rows) >= 2:
                kind = "returns" if c["r"] == fund_rows[0] else "benchmark_returns"
            kind = class_kind(kind, rp, target, has_target, tcode)
            if kind and out[kind][h] is None:
                out[kind][h] = evidence(float(number(c["text"])), n, c["text"])
                ds = dates(cp)
                if ds and out[kind]["as_of_date"] is None:
                    hn, raw = doc.header_raw(n, c, cp)
                    out[kind]["as_of_date"] = evidence(max(ds), hn, raw)
                elif out[kind]["as_of_date"] is None:
                    out[kind]["as_of_date"] = doc.returns_date(n)

    annual = {}
    for n in doc.tables:
        ctx = doc.context(n)
        text = compact(" ".join(c.get("text", "") for t in (n, *doc.continuation_headers(n))
                                for c in t.get("cells", [])))
        if not ("연도별수익률" in ctx or ("년차" in text and any(x in text for x in ("비교지수", "기초지수")))):
            continue
        title_date = doc.returns_date(n)
        entries = list(doc.entries(n))
        tcode = table_class(doc, n) if target else None
        has_target = bool(target) and (norm_class(tcode) == target or
                                       any(norm_class(class_code(rp)) == target for _, _, rp in entries))
        for c, cp, rp in entries:
            if len(set(re.findall(r"최근\s*\d년차", cp))) > 1:
                continue  # A merged header no longer identifies a unique year.
            ds = dates(cp)
            ym = re.search(r"(?:19|20)\d{2}(?=년|\b)", cp)
            inferred = False
            nth = re.search(r"최근\s*(\d+)\s*년차", cp)
            if not ds and not ym and nth and title_date:
                as_of = dt.date.fromisoformat(value(title_date))
                def years_before(n):
                    year = as_of.year - n
                    return as_of.replace(year=year, day=min(as_of.day, calendar.monthrange(year, as_of.month)[1]))
                count = int(nth[1])
                if count < 1:
                    continue
                ds = [(years_before(count) + dt.timedelta(days=1)).isoformat(), years_before(count - 1).isoformat()]
                inferred = True
            year = max(ds)[:4] if ds else ym[0] if ym else None
            kind = row_kind(rp)
            fallback = kind is None
            kind = class_kind(kind, rp, target, has_target, tcode)
            if not year or kind not in ("returns", "benchmark_returns"):
                continue
            hn, raw = doc.header_raw(n, c, cp)
            if inferred:
                hn, raw = n, cp + " | " + title_date["raw"]
            item = annual.setdefault(year, {"year": evidence(int(year), hn, raw),
                                           "fund": None, "benchmark": None, "period": None})
            key = "fund" if kind == "returns" else "benchmark"
            if fallback and item[key] is not None:
                continue  # 클래스 행 보완은 빈 칸만 채운다.
            item[key] = evidence(float(number(c["text"])), n, c["text"])
            if ds:
                item["period"] = evidence("/".join([min(ds), max(ds)]), hn, raw)
    out["annual_returns"] = [annual[y] for y in sorted(annual, reverse=True)] or None


def row_class(doc, n, r, rp):
    """class_code, plus a label split at a page break: '수수료후취-온라인슈퍼' | 다음 쪽 '(S)'."""
    code = class_code(rp)
    if code or "수수료" not in rp or r != len(n.get("grid", [])) - 1:
        return code
    for nxt in doc.tables:
        if nxt.get("continued_from") == n["id"] and nxt.get("grid"):
            first = [x for x in nxt["grid"][0] if x]
            m = re.fullmatch(r"\(\s*(" + CLASS_CODE + r")\s*\)", first[0].strip()) if len(first) == 1 else None
            return re.sub(r"\s+", "", m[1]) if m else None
    return None


def header_cells(n, c):
    return [h for h in n.get("cells", []) if h["r"] < c["r"] and h["c"] <= c["c"] < h["c"] + h.get("cs", 1)]


def inherited_headers(doc, n, c):
    """(table, header cell) pairs over column c; 머리글 없는 이어진 표는 앞 표의 머리글을 쓴다."""
    data_rows = {r for r, row in enumerate(n.get("grid", [])) if sum(number(x) is not None for x in row[1:]) >= 2}
    own = [h for h in header_cells(n, c) if h["r"] not in data_rows and h.get("text")]
    if own:
        return [(n, h) for h in own]
    for prior in doc.continuation_headers(n):
        prior_data = {r for r, row in enumerate(prior.get("grid", [])) if sum(number(x) is not None for x in row[1:]) >= 2}
        heads = [(prior, h) for h in prior.get("cells", []) if h["r"] not in prior_data and h.get("text")
                 and number(h["text"]) is None and h["c"] <= c["c"] < h["c"] + h.get("cs", 1) and h["c"] > 0]
        if heads:
            return heads
    return []


def merged_role(n, c, h):
    """Role of column c under a header naming several roles (예: '판매회사 신탁업자 보수').

    같은 글자의 머리글 칸이 역할 수만큼 나란히 있으면 순서대로 나눈다. 아니면 None.
    """
    roles = fee_roles(h.get("text"))
    cols = sorted({col for x in n.get("cells", []) if x["r"] == h["r"] and x.get("text") == h.get("text")
                   for col in range(x["c"], x["c"] + x.get("cs", 1))})
    if len(roles) >= 2 and len(cols) == len(roles) and cols == list(range(cols[0], cols[0] + len(cols))):
        return roles[cols.index(c["c"])]
    return None


PERIOD = re.compile(r"(최초설정일|\d{4}년\d{1,2}월\d{1,2}일)(?:로)?부터(\d{4}년\d{1,2}월\d{1,2}일)?")


def period_of(text):
    """'[2032년1월1일부터2036년12월31일까지]' → ('2032-01-01', '2036-12-31'). 시작이 최초설정일이면 None, 끝이 없으면 None."""
    found = list(PERIOD.finditer(compact(text or "")))
    if not found:
        return None
    m = found[-1]
    as_iso = lambda s: s and "{}-{:02d}-{:02d}".format(*map(int, re.findall(r"\d+", s)))
    return (None if m[1] == "최초설정일" else as_iso(m[1]), as_iso(m[2]))


def off_period_tables(doc, as_of):
    """기준일이 들지 않는 기간의 보수표 id. 기준일이 든 기간 표가 하나라도 있어야 쓴다 (없으면 빈 집합).

    기간 제목이 없는 표는 이어진 표이거나, 바로 앞 표와 같은 제목 아래(제목이 앞 표 제목의 앞부분)이면 앞 표의 기간을 잇는다.
    """
    if not as_of or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(as_of)):
        return set()
    periods, last, last_ctx = {}, None, ""
    for n in doc.tables:
        ctx = doc.context(n)
        p = period_of(ctx)
        if p is None and n.get("continued_from") in periods:
            p = periods[n["continued_from"]]
        elif p is None and last and len(ctx) > 10 and last_ctx.startswith(ctx):
            p = last
        if p is not None:
            periods[n["id"]] = p
        last, last_ctx = p, ctx
    inside = {i for i, (s, e) in periods.items() if (s or "") <= as_of and (e is None or as_of <= e)}
    return set(periods) - inside if inside else set()


BARE_ROLES = {"집합투자": "management", "운용": "management", "판매": "ap", "수탁": "trustee", "신탁": "trustee"}


def bare_role(n, h):
    """'집합투자 | 판매 | 수탁'처럼 역할 이름만 쓴 머리글. 같은 머리글 줄에 역할이 3개 이상일 때만 읽는다."""
    text = re.sub(r"[·ㆍᆞ•∙・.,()\[\]①-⑳]", "", compact(h.get("text")))
    if text not in BARE_ROLES:
        return None
    row = {compact(x.get("text")) for x in n.get("cells", []) if x["r"] == h["r"] and x.get("header")}
    roles = {BARE_ROLES.get(t) or fee_key_loose(t) or fee_key(t) for t in row} & {"management", "ap", "trustee", "admin"}
    return BARE_ROLES[text] if len(roles) >= 3 else None


def class_qualified(label, target):
    """받은 클래스 코드 바로 뒤에 한글 괄호 구분이 붙었는지 (예: 'S-P (퇴직)', '(S-P(퇴직))')."""
    return any(norm_class(m[1]) == target for m in re.finditer(
        r"(?<![A-Za-z])(" + CLASS_CODE + r")\s*\(\s*(?!수수료)[가-힣]", label or ""))


def costs(doc, out, target=None):
    candidates = []
    selected = {}
    for n in doc.tables:
        txt = compact(" ".join(c.get("text", "") for c in n.get("cells", [])))
        if n.get("continued_from") in selected:
            detailed = selected[n["continued_from"]]
            candidates.append((detailed, n))
            selected[n["id"]] = detailed
            continue
        trading_rate = "매매중개수수료율" in re.sub(r"[·ㆍᆞ•∙・]", "", txt)
        if "보수" not in txt and not trading_rate:
            continue
        if not (trading_rate or "보수및" in doc.context(n) or "지급비율" in txt or
                ("총보수" in txt and "판매수수료" in txt)):
            continue
        detailed = any(s in txt for s in ("집합투자업자보수", "일반사무관리", "신탁업자보수"))
        candidates.append((int(detailed), n))
        selected[n["id"]] = int(detailed)
    ordered = [n for _, n in sorted(candidates, key=lambda x: x[0], reverse=True)]
    fee_rows = {}
    off_period = off_period_tables(doc, value(out["dates"].get("as_of_date")))
    plain_rows = {}

    def qualified_sibling(n, r, rp):
        """'S-P(퇴직)'처럼 받은 클래스 뒤에 한글 구분이 붙은 행. 같은 표에 구분 없는 행이 있을 때만 True."""
        if not target or not class_qualified(rp, target):
            return False
        if n["id"] not in plain_rows:
            plain_rows[n["id"]] = any(
                norm_class(row_class(doc, n, rr, label)) == target and not class_qualified(label, target)
                for rr, label in {(c["r"], rp2) for c, _, rp2 in doc.entries(n)})
        return plain_rows[n["id"]]

    def usable(n, c, cp, rp):
        if re.search(r"\d+년", compact(cp)) or any(x in compact(cp + rp) for x in ("투자기간별", "예시", "지급시기", "년후")):
            return False
        # 기간별 보수표(TDF 등)는 기준일이 든 기간의 표만 쓴다. 기간 표가 없는 문서는 그대로.
        if n["id"] in off_period or qualified_sibling(n, c["r"], rp):
            return False
        # 받은 클래스가 주어지면 그 클래스로 확인되는 행만 쓴다. 클래스 표시가 없는 행은
        # 어느 클래스 값인지 알 수 없어 쓰지 않는다 (meta가 없으면 기존 규칙 그대로).
        if not target or norm_class(row_class(doc, n, c["r"], rp)) == target:
            return True
        # 뒤집힌 표: 클래스가 열 머리글에 있고 행은 보수 이름(집합투자업자보수 …)이다.
        if (class_code(rp) is None and fee_key(rp)
                and any(norm_class(class_code(p)) == target for p in (c.get("col_path") or []))):
            return True
        # 모든 클래스에 같은 값이라 세로로 합쳐진 칸은 덮는 행 중에 받은 클래스가 있으면 쓴다.
        g = n.get("grid", [])
        rows = range(c["r"] + 1, min(c["r"] + c.get("rs", 1), len(g)))
        return any(norm_class(class_code(" ".join(x for x in g[r][:c["c"]] if x and number(x) is None))) == target
                   for r in rows)

    def inherited_key(n, c, strict):
        """Key from the nearest header over c (이어진 표는 앞 표의 머리글). 합쳐진 머리글은 칸 위치로 나눈다."""
        heads = sorted(inherited_headers(doc, n, c), key=lambda th: th[1]["r"])
        if not heads:
            return None
        ht, h = heads[-1]
        if (n["id"], c["r"], c["c"]) in merged:
            return merged_role(ht, c, h)
        key = ((strict and fee_key(h.get("text", ""))) or fee_key_loose(h.get("text", ""))
               or (bare_role(ht, h) if target else None))
        if key is None and target:
            # 머리글 끝줄('보수')만 다음 쪽 표로 넘어간 경우: 바로 위 머리글(앞 쪽 표 포함)과 이어 읽는다.
            above = [hh for _, hh in heads[:-1]]
            prior = doc.by_id.get(n.get("continued_from"))
            if not above and prior and ht is n:
                # 앞 표의 마지막 줄 머리글은 아래가 다음 쪽이라 머리글 표시가 빠질 수 있다.
                above = [x for x in prior.get("cells", []) if x.get("text")
                         and number(x["text"]) is None and sum(number(v) is not None for v in prior["grid"][x["r"]][1:]) < 2 and x["c"] <= c["c"] < x["c"] + x.get("cs", 1)]
                above.sort(key=lambda x: x["r"])
            if above:
                key = fee_key(compact(above[-1].get("text", "")) + compact(h.get("text", "")))
        return key

    def put(key, n, c, rp):
        out["fees"][key] = evidence(float(number(c["text"])), n, c["text"])
        fee_rows[key] = (n, rp)

    merged, blocked = set(), set()

    def classify(n, c):
        for _, h in inherited_headers(doc, n, c):
            text = compact(h.get("text"))
            roles = sum(word in text for word in ("집합투자", "지정참가", "신탁", "사무관리"))
            if ((roles > 1 and "보수" in text)
                    or ("동종" in text and "지정참가" in text and "판매수수료" in text)
                    or ("동종" in text and ("합성" in text or "피투자" in text))
                    or ("피투자" in text and "거래" in text)):
                blocked.add((n["id"], c["r"], c["c"]))
            elif len(fee_roles(text)) > 1:
                # OCR이 이웃 머리글을 합친 칸: 한 역할로 읽으면 다른 보수가 들어간다.
                merged.add((n["id"], c["r"], c["c"]))
        if any(len(fee_roles(p)) > 1 and "지정참가" not in compact(p) for p in (c.get("col_path") or [])[-1:]):
            merged.add((n["id"], c["r"], c["c"]))

    for n in ordered:
        for c, cp, rp in doc.entries(n):
            if not usable(n, c, cp, rp):
                continue
            classify(n, c)
            if (n["id"], c["r"], c["c"]) in blocked | merged:
                continue
            paths = c.get("col_path") or [h.get("text", "") for h in header_cells(n, c)]
            key = next((fee_key(p) for p in reversed(paths) if fee_key(p)), None) or fee_key(rp)
            if key is None and target:
                # 받은 클래스 행은 머리글 없는 이어진 표에 있는 경우가 많다.
                key = inherited_key(n, c, strict=True)
            if key and out["fees"][key] is None:
                put(key, n, c, rp)
            labels = re.sub(r"[·ㆍᆞ•∙・]", "", compact(cp + rp))
            if out["trading_cost"] is None and any(s in labels for s in
                    ("매매중개", "증권거래비용", "매매수수료")):
                out["trading_cost"] = evidence(float(number(c["text"])), n, c["text"])

    # 2단계: 기존 규칙이 비워 둔 보수만 채운다. 합쳐진 머리글은 칸 위치로 나누고,
    # '보수'가 빠진 머리글·흔한 OCR 오타를 받으며, '지급비용' 머리글의 상세표도 본다.
    extra = [n for n in doc.tables if n not in ordered and "지급비용" in compact(
             " ".join(c.get("text", "") for c in n.get("cells", [])[:40]))
             and len({fee_key(c.get("text", "")) for c in n.get("cells", []) if c.get("header")} - {None}) >= 2]
    extra += [n for n in doc.tables if n not in ordered and n not in extra
              and any(p in extra for p in doc.continuation_headers(n))]
    for n in ordered + extra:
        for c, cp, rp in doc.entries(n):
            if not usable(n, c, cp, rp):
                continue
            if n in extra:
                classify(n, c)
            if (n["id"], c["r"], c["c"]) in blocked:
                continue
            key = inherited_key(n, c, strict=bool(target) or n in extra)
            if key and out["fees"][key] is None:
                put(key, n, c, rp)
    return fee_rows


def financials(doc, out):
    periods = {}
    for n in doc.tables:
        ctx = doc.context(n)
        if any(s in ctx for s in ("관련회사", "집합투자업자에관한", "집합투자업자에대한")):
            continue
        if not any(s in ctx + compact(" ".join((n.get("grid") or [[]])[0]))
                   for s in ("재무", "손익계산서")):
            continue
        for c, cp, rp in doc.entries(n):
            label = compact(rp)
            label = re.sub(r"^[IVXivxⅠ-Ⅹ\d.()]+", "", label)
            key = None
            for field, terms in {
                "assets": ("자산총계", "자산총액"), "liabilities": ("부채총계", "부채총액"),
                "net_assets": ("자본총계", "순자산총계", "순자산총액"),
                "operating_revenue": ("운용수익",), "net_income": ("당기순이익", "당기순손익")
            }.items():
                if any(label.startswith(t) for t in terms):
                    key = field
                    break
            if label == "순자산":
                key = "net_assets"
            ds = dates(cp)
            # Multiple fiscal dates in a single OCR header are ambiguous unless
            # they form one explicitly delimited reporting interval.
            if len(set(ds)) > 1 and (not re.search(r"[~∼〜]", cp) or len(set(ds)) > 2):
                continue
            if key is None or not ds:
                continue
            if key in ("net_income", "operating_revenue") and "재무상태표" in ctx and "손익계산서" not in ctx:
                continue
            period = max(ds)
            unit = doc.unit(n)
            # Never silently join financial amounts reported in different units.
            pk = (period, value(unit))
            hn, raw = doc.header_raw(n, c, cp)
            item = periods.setdefault(pk, {"period": evidence(period, hn, raw),
                "assets": None, "liabilities": None, "net_assets": None,
                "operating_revenue": None, "net_income": None, "unit": unit})
            if item[key] is None:
                item[key] = amount(c["text"], n, unit)
    selected = sorted(periods.values(), key=lambda p: (value(p["period"]),
                      sum(p.get(k) is not None for k in ("assets", "liabilities", "net_assets", "operating_revenue", "net_income"))), reverse=True)
    # Prefer summary tables on ties (encountered first), at most 3 distinct dates.
    latest = {}
    for item in selected:
        latest.setdefault(value(item["period"]), item)
    out["financials"] = list(latest.values())[:3] or None


def history(doc, out):
    rows = {}
    for n in doc.tables:
        txt = compact(" ".join(c.get("text", "") for c in n.get("cells", [])))
        if not ("기간초" in txt and "기간말" in txt and "환매" in txt):
            continue
        grid = n.get("grid", [])
        for c, cp, rp in doc.entries(n):
            labels = compact(cp)
            if "금액" not in labels:
                continue
            if "환매" in labels and ("설정" in labels or "발행" in labels):
                continue
            r = c["r"]
            period_raw = next((s for s in grid[r] if dates(s)), "") if r < len(grid) else ""
            ds = dates(period_raw)
            if not ds:
                continue
            period = "/".join([min(ds), max(ds)])
            key = ("begin_amount" if "기간초" in labels else
                   "end_amount" if "기간말" in labels else
                   "redemption_amount" if "환매" in labels else
                   "subscription_amount" if "설정" in labels or "발행" in labels else None)
            if not key:
                continue
            item = rows.setdefault(period, {"period": evidence(period, n, period_raw),
                "begin_amount": None, "subscription_amount": None,
                "redemption_amount": None, "end_amount": None, "unit": doc.unit(n)})
            item[key] = amount(c["text"], n, doc.unit(n))
    out["asset_history"] = [rows[p] for p in sorted(rows, key=lambda s: s.split("/")[-1], reverse=True)] or None


FUND_TYPE_LABEL = r"(?:주식혼합|채권혼합|재간접|주식|채권|혼합)(?:형)?"


def fund_type_evidence(doc, fund_name):
    """Prefer classification rows; accept only known asset-type labels."""
    pattern = r"증권(?:형)?[\[(](" + FUND_TYPE_LABEL + r")[\])]"
    for n in doc.tables:
        for row in n.get("grid", []):
            if any(compact(cell).strip(":：") == "분류" for cell in row):
                for raw in row:
                    m = re.search(pattern, compact(raw))
                    if m:
                        label = m[1] if m[1].endswith("형") else m[1] + "형"
                        return evidence("증권(" + label + ")", n, raw)
    for n, raw in doc.texts:
        m = re.search(pattern, compact(raw))
        if m:
            label = m[1] if m[1].endswith("형") else m[1] + "형"
            return evidence("증권(" + label + ")", n, raw)
    if fund_name:
        m = re.search(r"[\[(](" + FUND_TYPE_LABEL + r")[\])]$", compact(value(fund_name)))
        if m:
            label = m[1] if m[1].endswith("형") else m[1] + "형"
            return {**fund_name, "value": "증권(" + label + ")"}
    return None


def benchmark_name(raw):
    """Cut separators/weights only outside balanced index qualifiers."""
    depth = 0
    end = len(raw)
    for i, char in enumerate(raw):
        if depth == 0 and (char in ",;\n" or
                           re.match(r"\s*[×x*]\s*\d+%|\s+\d+%", raw[i:])):
            end = i
            break
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return None
    if depth:
        return None
    return re.sub(r"(?:100(?:\.0+)?|\d{1,2}(?:\.\d+)?)%$", "", raw[:end].strip()).strip()


def inception_period(tail):
    """A period starting at inception does not disclose the inception date."""
    return bool(re.match(r"[:：]?(?:[~～〜∼–—-]|부터|까지|로부터|이후|이전)", compact(tail)))


def history_inception(doc):
    for n, raw in doc.texts:
        s = compact(raw)
        # OCR may flatten a dated history row into a paragraph.
        m = re.fullmatch(r"(.+?)최초설정(?:일)?[.]?", s)
        if m and len(dates(m[1])) == 1:
            return evidence(dates(m[1])[0], n, raw)
    for n in doc.tables:
        for row in n.get("grid", []):
            if any(compact(cell) == "최초설정" for cell in row):
                raw = " ".join(dict.fromkeys(row))
                found = dates(raw)
                if len(found) == 1:
                    return evidence(found[0], n, raw)
    return None


def identity_classification(doc, out):
    ident, cls, ds = out["identity"], out["classification"], out["dates"]
    for k in ("fund_name", "fund_code"):
        ident[k] = doc.metadata(k)
    if ident["fund_name"]:
        name = re.sub(r"^이\s*투자설명서는\s*", "", value(ident["fund_name"]))
        code = value(ident["fund_code"])
        if code:
            name = re.sub(r"\(" + re.escape(code) + r"\)$", "", name)
        ident["fund_name"]["value"] = name
        if compact(name).startswith("증권상장지수"):
            ident["fund_name"] = None
        # 본문 문장 조각(기준가격 산정 설명)이나 모투자신탁 이름은 버리고 아래 규칙으로 다시 찾는다.
        elif re.search(r"당해종류|상당액|계상된|재무상태표|모투자신탁", compact(name)):
            ident["fund_name"] = None
    ds["as_of_date"] = doc.metadata("as_of_date")
    if ds["as_of_date"] and dates(value(ds["as_of_date"])):
        ds["as_of_date"]["value"] = dates(value(ds["as_of_date"]))[0]
    source = doc.data.get("source", {})
    raw = source.get("file") if isinstance(source, dict) else source
    ident["source"] = {"value": raw, "page": None, "node": "source.file", "raw": raw} if raw else None
    for n, raw in doc.texts:
        s = compact(raw)
        if not out["risk_grade"]:
            m = re.search(r"투자위험등급[:：]?([1-6])등급|([1-6])등급으로분류", s)
            if m:
                out["risk_grade"] = evidence(int(m[1] or m[2]), n, raw)
        if not ident["fund_name"]:
            m = re.search(r"집합투자기구(?:의)?명칭[:：]?(.{3,100}?)(?=\d[.]|집합투자업자|한국금융|$)", s)
            if m and "투자" in m[1] and not re.search(r"합니다|바랍니다|사항|명칭", m[1]):
                ident["fund_name"] = evidence(m[1].strip(":"), n, raw)
            # A short stand-alone fund-name cell is another title-page source.
            elif len(s) <= 90 and re.fullmatch(r"[가-힣A-Za-z0-9+&()\[\]·-]+(?:증권)?상장지수(?:증권)?투자신탁[\[(][가-힣]+[\])]", s):
                if not s.startswith("증권상장"):
                    ident["fund_name"] = evidence(s, n, raw)
        if not ident["manager"]:
            company = r"(?:주식회사|\(주\))?[가-힣A-Za-z-]{2,25}(?:자산|투자신탁)운용(?:주식회사|\(주\))?"
            m = re.search(r"(?:집합투자업자명칭|집합투자업자|운용회사)[:：]?(" + company + r")", s)
            if m:
                ident["manager"] = evidence(m[1], n, raw)
            elif re.fullmatch(company, s):
                ident["manager"] = evidence(s, n, raw)
        for key, pattern in (("inception_date", r"최초설정일|최초설정|설정일자"),
                             ("listing_date", r"상장일(?:자)?[:：]?")):
            if ds[key] is None:
                m = re.search(pattern, s)
                if m:
                    tail = s[m.end():]
                    if key == "inception_date" and inception_period(tail):
                        continue
                    found = dates(tail[:25])
                    if found:
                        ds[key] = evidence(found[0], n, raw)
        if cls["currency_hedge"] is None and "환헤지" in s:
            m = re.search(r"환헤지.{0,30}(실시하지않|수행하지않|하지않|미실시|하지아니|없음|실시할계획이없)", s)
            yes = re.search(r"환헤지.{0,20}(실시합니다|수행합니다|실시함|실시할예정|실시할계획|적용합니다)", s)
            if m or yes:
                cls["currency_hedge"] = evidence(False if m else True, n, raw)
        if cls["min_main_asset_pct"] is None and "주식" in s:
            for m in re.finditer(r"(\d+(?:\.\d+)?)%이상", s):
                area = s[max(0, m.start()-60):m.end()+50]
                if "주식" in area and any(x in area for x in ("투자", "편입")) and "시가총액" not in area:
                    cls["min_main_asset_pct"] = evidence(float(m[1]), n, raw)
                    break
        if cls["benchmark_index"] is None:
            # Explicit labelled index names; stop before strategy prose.
            m = re.search(r"(?:기초\s*지수|추적\s*대상\s*지수|비교\s*지수)(?:\s*\([^)]*지수\))?\s*[:：]\s*([A-Za-z가-힣0-9][A-Za-z가-힣0-9&+().,/_% -]{1,65})", raw.replace("\n", " "))
            if not m:
                m = re.search(r"([A-Za-z][A-Za-z0-9 ]{1,35}(?:지수|Index))[^.]{0,8}(?:를|을)(?:기초지수|추적대상지수|비교지수)", s)
            if not m:
                m = re.search(r'''["“‘']([^"”’']{2,65})["”’'](?:의)?수익률(?:을|를)추적''', s)
            if m:
                name = benchmark_name(m[1])
                if name and len(name) <= 65 and not re.search(r"합니다|투자신탁|없음|사용하고|수익률|변경|선정|하여", name):
                    cls["benchmark_index"] = evidence(name, n, raw)
    cls["fund_type"] = fund_type_evidence(doc, ident["fund_name"])
    # A labelled 100% index target establishes 1x; absence of leverage wording
    # alone does not establish a multiplier.
    fund_name = value(ident["fund_name"]) or ""
    if re.search(r"레버리지|인버스", fund_name):
        mult = -2 if "인버스" in fund_name and re.search(r"2X|2배|레버리지", fund_name, re.I) else -1 if "인버스" in fund_name else 2
        cls["leverage"] = {**ident["fund_name"], "value": mult}
    else:
        implied = None
        for n, raw in doc.texts:
            s = compact(raw)
            m = re.search(r"(?:추적대상지수|기초지수|비교지수).{0,50}?(?:[x×*]|배율[:：]?)(-?[12])(?:[.]0)?배?", s, re.I)
            if m and ("투자목적" in doc.context(n) or "추적대상지수:" in s):
                cls["leverage"] = evidence(int(m[1]), n, raw)
                break
            multiple = re.search(r"(?:지수|변동률).{0,50}?(-?[12])(?:[.]0)?배(?:수)?(?:와|를|의|수준)", s)
            if multiple and any(x in s for x in ("유사", "추종", "추적", "투자목적")):
                cls["leverage"] = evidence(int(multiple[1]), n, raw)
                break
            if (re.search(r"(?:추적대상지수|기초지수|비교지수)(?:\([^)]*지수\))?[:：].{1,50}100%", s)
                    or re.search(r"(?:추적대상지수|기초지수)(?:인.{1,40}?지수)?의변동률과(?:동일|유사)", s)):
                implied = implied or evidence(1, n, raw)
        if cls["leverage"] is None:
            cls["leverage"] = implied


def extract(data, meta=None):
    """meta: 문서 밖에서 받은 정보. {"share_class": "S-P"}이면 그 클래스 행의 보수·수익률을 쓴다."""
    doc = Document(data)
    share_class = (meta or {}).get("share_class")
    target = norm_class(share_class)
    out = {"identity": dict.fromkeys(("fund_name", "fund_code", "manager", "source", "share_class")),
           "risk_grade": None,
           **{k: dict.fromkeys((*HORIZONS, "as_of_date")) for k in ("returns", "benchmark_returns", "volatility")},
           "annual_returns": None, "fees": dict.fromkeys(FEE_KEYS), "trading_cost": None,
           "financials": None, "asset_history": None,
           "classification": dict.fromkeys(("benchmark_index", "fund_type", "leverage", "currency_hedge", "min_main_asset_pct")),
           "dates": dict.fromkeys(("inception_date", "listing_date", "as_of_date"))}
    identity_classification(doc, out)
    performance(doc, out, target)
    if out["dates"]["inception_date"] is None:
        out["dates"]["inception_date"] = history_inception(doc)
    fee_rows = costs(doc, out, target)
    # 값을 읽은 클래스. 받은 클래스가 없으면 총보수를 읽은 행의 클래스를 적는다 (ETF는 보통 null).
    if share_class:
        out["identity"]["share_class"] = {"value": share_class, "page": None,
                                          "node": "meta.share_class", "raw": str(share_class)}
    elif "total" in fee_rows:
        n, rp = fee_rows["total"]
        code = class_code(rp)
        if code and code != "?":
            out["identity"]["share_class"] = evidence(code, n, rp)
    financials(doc, out)
    # 규모 축에 필요한 순자산만 남긴다. 금액은 원 단위로 합쳐 값 하나로 저장한다.
    net_assets = []
    for item in out.pop("financials") or []:
        value_item = item.get("net_assets")
        if value_item and value_item.get("value_krw") is not None:
            unit = (item.get("unit") or {}).get("value", "")
            net_assets.append({"period": item["period"],
                               "amount": {"value": value_item["value_krw"], "page": value_item["page"],
                                         "node": value_item["node"], "raw": f"{value_item['raw']} {unit}".strip()}})
    out["net_assets"] = net_assets or None
    out.pop("asset_history", None)
    out["identity"].pop("fund_code", None)
    out["classification"].pop("min_main_asset_pct", None)
    return out


def extract_file(path):
    if not path.name.endswith(".ocr_structure.json"):
        raise ValueError("Input must be *.ocr_structure.json (OCR only)")
    data = json.loads(path.read_text(encoding="utf-8"))
    stem = path.name.removesuffix(".ocr_structure.json")
    meta_path = path.with_name(stem + ".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) if meta_path.is_file() else None
    result = extract(data, meta)
    target = path.with_name(stem + ".fields.json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(target.relative_to(ROOT) if target.is_relative_to(ROOT) else target)
    return target


def flatten(data, prefix=""):
    if data is None or isinstance(data, dict) and "value" in data:
        leaves = {prefix: data}
        if isinstance(data, dict) and "value_krw" in data:
            leaves[prefix + ".value_krw"] = (dict(data, value=data["value_krw"])
                                            if data["value_krw"] is not None else None)
        return leaves
    result = {}
    for key, item in (enumerate(data) if isinstance(data, list) else data.items()):
        result.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    return result


# 컬럼 설명: (설명, 단위). 목록 항목의 숫자 번호(.0., .1.)는 0이 가장 최근 기간이다.
COLUMN_DOCS = {
    "identity.fund_name": ("펀드 정식 명칭", ""),
    "identity.manager": ("집합투자업자(운용사)", ""),
    "identity.source": ("원본 투자설명서 파일명", ""),
    "identity.share_class": ("보수·수익률을 읽은 클래스 (meta.json으로 받은 값, 없으면 총보수를 읽은 행의 클래스. ETF는 보통 비어 있음)", ""),
    "risk_grade": ("투자위험등급. 1=매우 높은 위험 ~ 6=매우 낮은 위험", "등급"),
    "returns": ("펀드 연평균 수익률 (핵심요약 투자실적 표)", "%"),
    "benchmark_returns": ("비교지수 연평균 수익률 (같은 표)", "%"),
    "volatility": ("수익률 변동성, 연환산 표준편차 (같은 표)", "%"),
    "1y": ("최근 1년", ""), "2y": ("최근 2년", ""), "3y": ("최근 3년", ""), "5y": ("최근 5년", ""),
    "since_inception": ("설정일 이후", ""),
    "as_of_date": ("수치의 기준일 (표 머리글 기간의 끝 날짜)", "날짜"),
    "fees.management": ("집합투자업자(운용) 보수", "연 %"),
    "fees.ap": ("지정참가회사 보수 (ETF) / 판매회사 보수 (일반 펀드)", "연 %"),
    "fees.trustee": ("신탁업자(수탁) 보수", "연 %"),
    "fees.admin": ("일반사무관리회사 보수", "연 %"),
    "fees.total": ("총보수 (위 보수의 합계)", "연 %"),
    "fees.other_expenses": ("기타비용 (보수 외 비용)", "연 %"),
    "fees.ter": ("총보수·비용 (총보수 + 기타비용)", "연 %"),
    "fees.peer_total": ("동종유형 총보수 (같은 유형 펀드 평균, ETF만의 평균 아님)", "연 %"),
    "fees.synthetic_ter": ("합성총보수·비용 (피투자펀드 보수 포함)", "연 %"),
    "trading_cost": ("매매·중개수수료율 (증권 거래비용)", "연 %"),
    "classification.benchmark_index": ("기초지수(추적대상지수) 이름", ""),
    "classification.fund_type": ("펀드 유형 (예: 증권(주식형))", ""),
    "classification.leverage": ("지수 대비 배율. 1=일반, 2=레버리지, -1=인버스, -2=인버스2X", "배"),
    "classification.currency_hedge": ("환헤지 여부 (true/false)", ""),
    "dates.inception_date": ("최초 설정일", "날짜"),
    "dates.listing_date": ("최초 상장일", "날짜"),
    "dates.as_of_date": ("투자설명서 작성 기준일", "날짜"),
    "net_assets": ("순자산 (재무상태표 자본총계), 기간별 (0=최근 회계기간)", ""),
    "net_assets.period": ("순자산 - 회계기간 종료일", "날짜"),
    "net_assets.amount": ("순자산 - 금액", "원"),
    "annual_returns": ("연도별 수익률 (값이 없으면 빈 목록)", ""),
    "annual_returns.year": ("연도별 수익률 - 연도", ""),
    "annual_returns.fund": ("연도별 수익률 - 펀드 수익률", "%"),
    "annual_returns.benchmark": ("연도별 수익률 - 비교지수 수익률", "%"),
    "annual_returns.period": ("연도별 수익률 - 산정 기간 (시작/끝)", "날짜"),
}
SUFFIX_DOCS = {
    "value_krw": ("원 단위로 환산한 값", "원"),
    "page": ("값을 읽은 투자설명서 쪽 번호", "쪽"),
    "node": ("값을 읽은 OCR 구조 노드 ID (t로 시작하면 표)", ""),
    "raw": ("OCR이 읽은 원래 글자", ""),
}


def describe(column):
    """컬럼 이름을 (설명, 단위)로 푼다. 목록 번호와 .page/.node/.raw/.value_krw 접미사를 해석한다."""
    parts = column.split(".")
    suffixes = []
    while parts and parts[-1] in SUFFIX_DOCS:
        suffixes.insert(0, parts.pop())
    index = next((p for p in parts if p.isdigit()), None)
    key = ".".join(p for p in parts if not p.isdigit())
    if key in COLUMN_DOCS:
        text, unit = COLUMN_DOCS[key]
    else:
        group, _, leaf = key.rpartition(".")
        group_doc = COLUMN_DOCS.get(group, (group, ""))
        leaf_doc = COLUMN_DOCS.get(leaf, (leaf, ""))
        text, unit = f"{group_doc[0]} - {leaf_doc[0]}", leaf_doc[1] or group_doc[1]
    if index is not None:
        text += " [최근 기간]" if index == "0" else f" [{index}기 전]"
    for suffix in suffixes:
        text = f"{text} → {SUFFIX_DOCS[suffix][0]}"
        unit = SUFFIX_DOCS[suffix][1] if suffix != "value_krw" or not unit else "원"
        if suffix in ("page", "node", "raw"):
            unit = SUFFIX_DOCS[suffix][1]
    return text, unit


def coverage_values(record):
    result = {}
    for path, item in flatten(record).items():
        key = re.sub(r"\.\d+\.", ".", path)
        if item is not None:
            result.setdefault(key, []).append(str(value(item)).lower() if isinstance(value(item), bool) else str(value(item)))
        else:
            if re.search(r"\.\d+\.", path):
                result.setdefault(key, []).append("-")
            else:
                result.setdefault(key, [])
    return result


def rebuild_datalake(output=OUTPUT):
    """Rebuild only from fields.json whose corresponding OCR document exists."""
    paths = [p.with_name(p.name.removesuffix(".ocr_structure.json") + ".fields.json")
             for p in sorted(output.glob("*/*.ocr_structure.json"))]
    missing = [p for p in paths if not p.is_file()]
    records = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in paths if p.is_file()]
    lake = output / "datalake"
    lake.mkdir(parents=True, exist_ok=True)
    (lake / "etf_fields.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for _, r in records), encoding="utf-8")
    flat = [flatten(r) for _, r in records]
    keys = list(dict.fromkeys(k for row in flat for k in row))
    # CSV는 값과 쪽만 둔다 (노드 ID와 원래 글자는 문서별 fields.json에 남아 있다).
    # 원 환산값은 원래 값과 같은 쪽에서 나오므로 쪽 컬럼을 따로 두지 않는다.
    columns = [part for k in keys for part in ((k,) if k.endswith(".value_krw") else (k, k + ".page"))]
    with (lake / "etf_fields.csv.raw").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in flat:
            writer.writerow({col: val for k, item in row.items() if item is not None
                             for col, val in ((k, item["value"]), (k + ".page", item["page"]))
                             if col in columns})
    # 컬럼 사전: CSV 컬럼마다 한국어 설명과 단위
    with (lake / "etf_fields_columns.csv.raw").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["컬럼", "설명", "단위"])
        for column in columns:
            writer.writerow([column, *describe(column)])
    cov = [coverage_values(r) for _, r in records]
    fields = list(dict.fromkeys(k for row in cov for k in row))
    # Empty collections are represented by their leaf fields in populated docs.
    fields = [k for k in fields if not any(x.startswith(k + ".") for x in fields)]
    names = [p.name.removesuffix(".fields.json") for p, _ in records]
    escape = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    lines = ["# ETF field coverage", "", "Values are OCR disclosures; '-' means missing. List values are newest first, separated by ';'.", "",
             "| field | 설명 | 단위 | " + " | ".join(escape(n) for n in names) + " |",
             "| --- | --- | --- | " + " | ".join("---" for _ in names) + " |"]
    for k in fields:
        cells = [";".join(row.get(k, [])) if any(v != "-" for v in row.get(k, [])) else "-" for row in cov]
        text, unit = describe(k)
        lines.append("| " + k + " | " + escape(text) + " | " + escape(unit) + " | "
                     + " | ".join(escape(cell) for cell in cells) + " |")
    if missing:
        lines.extend(["", "Missing fields files: " + ", ".join(p.name for p in missing)])
    (lake / "coverage.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.all == bool(args.path):
        parser.error("Specify an OCR structure path or --all")
    if args.all:
        paths = sorted(OUTPUT.glob("*/*.ocr_structure.json"))
        if not paths:
            parser.error("No OCR structures found")
        for path in paths:
            extract_file(path)
        print(f"Datalake: {rebuild_datalake()} documents")
    else:
        extract_file(args.path.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
