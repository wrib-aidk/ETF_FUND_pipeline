"""Small offline checks for extraction's highest-risk decisions."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extract_fields import extract, number, rebuild_datalake, fee_key, class_code, spread_role, fee_key_loose
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ETF_FUND_postprocess"))
from build_doc_structure import document_info
from build_doc_structure_ocr import (split_merged_headers, split_merged_rows,
                                     merge_wrapped_header_dates, correct_single_cells,
                                     split_fee_header_pieces, text_holes)


def table(ident, grid, page=1, section=None, unit=None):
    return {"id": ident, "type": "table", "page": page, "section": section,
            "n_cols": len(grid[0]), "grid": grid, "unit": unit,
            "cells": [{"r": r, "c": c, "rs": 1, "cs": 1, "text": text}
                      for r, row in enumerate(grid) for c, text in enumerate(row)]}


class ExtractionTests(unittest.TestCase):
    def test_spread_role_merged_neighbour_headers(self):
        # OCR이 '집합투자/판매회사/수탁회사' 윗줄을 가운데 칸에 붙이고 양옆에는 조각만 남긴 머리글
        heads = ["클래스", "업자 보수", "집합투자판매회사수탁회사 보수", "보수", "일반사무 관리회사 보수"]
        n = {"cells": [{"r": 0, "c": c, "cs": 1, "header": True, "text": t} for c, t in enumerate(heads)]}
        roles = [spread_role(n, {"c": c}, n["cells"][c]) for c in range(5)]
        self.assertEqual(roles, [None, "management", "ap", "trustee", None])
        # 양옆이 조각이 아니면(문장 칸) 나누지 않는다.
        n["cells"][3]["text"] = "보수 인하"
        self.assertIsNone(spread_role(n, {"c": 2}, n["cells"][2]))

    def test_fee_header_ocr_typos(self):
        self.assertEqual(fee_key_loose("완리회자 일반산문 '보수'"), "admin")
        self.assertEqual(fee_key_loose("신탈 업자 보수"), "trustee")
        self.assertIsNone(fee_key_loose("판매 수수료"))

    def test_fee_dot_variants_and_sum(self):
        for dot in ("·", "ㆍ", "•", "∙", "・", "ᆞ"):
            self.assertEqual(fee_key("총보수 " + dot + "비용"), "ter")
        self.assertEqual(fee_key("투자신탁보수 합계"), "total")

    def test_explicit_fee_continuation(self):
        head = table("h", [["구분", "지급비율"], ["총보수", ".04"]], section="fee")
        tail = table("t", [["증권 거래비용", ".0123"]], page=2)
        tail["continued_from"] = "h"
        r = extract({"nodes": [head, tail], "sections": [{"id": "fee", "path": ["보수 및 수수료"]}]})
        self.assertEqual(r["trading_cost"]["value"], .0123)
        self.assertEqual(r["trading_cost"]["node"], "t")

    def test_annual_continuation_and_underlying_index(self):
        head = table("h", [["기간", "최근1년차"], ["기간", "2023.07.02~2024.07.01"]])
        tail = table("t", [["투자신탁", "12"], ["기초지수", "11"]], page=2)
        tail["continued_from"] = "h"
        item = extract({"nodes": [head, tail]})["annual_returns"][0]
        self.assertEqual(item["year"]["value"], 2024)
        self.assertEqual(item["period"]["value"], "2023-07-02/2024-07-01")
        self.assertEqual(item["benchmark"]["value"], 11)
        self.assertEqual(item["fund"]["node"], "t")
        self.assertEqual(item["period"]["node"], "h")

    def test_caption_dates_and_annual_periods(self):
        average = table("a", [["구분", "최근1년"], ["투자신탁", "10"], ["비교지수", "9"]])
        annual = table("b", [["구분", "최근1년차", "최근2년차"], ["투자신탁", "10", "8"], ["비교지수", "9", "7"]])
        for t in (average, annual):
            t["caption"] = "수익률(세전 기준, 기준일:2024년 02월 29일, 단위:%)"
        r = extract({"nodes": [average, annual]})
        self.assertEqual(r["returns"]["as_of_date"]["value"], "2024-02-29")
        self.assertEqual(r["annual_returns"][0]["period"]["value"], "2023-03-01/2024-02-29")
        self.assertEqual(r["annual_returns"][1]["period"]["value"], "2022-03-01/2023-02-28")
        self.assertIn("기준일", r["annual_returns"][0]["period"]["raw"])

    def test_target_annual_without_benchmark_and_short_years(self):
        annual = table("a", [["연도", "최근 1년차", "최근 2년차"],
                             ["기간", "25.06.01~26.05.31", "25.02.26~25.05.31"],
                             ["종류A 수수료선취", "99", "88"],
                             ["종류S 수수료후취-온라인슈퍼", "56.47", "-"]], section="perf")
        data = {"nodes": [annual], "sections": [{"id": "perf", "path": ["집합투자기구의 운용실적"]}]}
        item, = extract(data, {"share_class": "S"})["annual_returns"]
        self.assertEqual((item["year"]["value"], item["fund"]["value"]), (2026, 56.47))
        self.assertEqual(item["period"]["value"], "2025-06-01/2026-05-31")
        self.assertIsNone(extract(data)["annual_returns"])

    def test_target_annual_incomplete_end_uses_as_of(self):
        annual = table("a", [["종류", "최근 1년차", "최근 2년차"],
                             ["기간", "2025/05/21 2026/05/2", "2024/05/21 2025/05/"],
                             ["Class S-P", "99", "88"], ["Class S", "33.64", "4.15"]], section="perf")
        data = {"document": {"as_of_date": "2026.05.20"}, "nodes": [annual],
                "sections": [{"id": "perf", "path": ["집합투자기구의 운용실적"]}]}
        items = extract(data, {"share_class": "S"})["annual_returns"]
        self.assertEqual([(i["year"]["value"], i["fund"]["value"], i["period"]["value"]) for i in items],
                         [(2026, 33.64, "2025-05-21/2026-05-20"), (2025, 4.15, "2024-05-21/2025-05-20")])

    def test_target_ter_conjunction_in_inherited_header(self):
        head = table("h", [["종류", "총보수", "기타비용", "총보수 및 비용"],
                           ["A", "1.0", ".02", "1.02"]], section="fee")
        tail = table("t", [["S", ".5", ".0043", ".5043"], ["S-P", ".4", ".004", ".404"]], page=2)
        tail["continued_from"] = "h"
        data = {"nodes": [head, tail], "sections": [{"id": "fee", "path": ["보수 및 수수료"]}]}
        fees = extract(data, {"share_class": "S"})["fees"]
        self.assertEqual((fees["total"]["value"], fees["ter"]["value"], fees["ter"]["node"]), (.5, .5043, "t"))
        self.assertIsNone(extract(data)["fees"]["ter"])

    def test_benchmark_labels_and_quotes(self):
        for text, expected in [('거래소가 발표하는 "SAMPLE123"의 수익률을 추적', "SAMPLE123"),
                               ("기초지수(추적대상지수):SAMPLE123100%", "SAMPLE123")]:
            r = extract({"nodes": [{"id": "n", "type": "paragraph", "page": 1, "text": text}]})
            self.assertEqual(r["classification"]["benchmark_index"]["value"], expected)

    def test_inception_periods_require_independent_evidence(self):
        period = {"id": "period", "type": "paragraph", "page": 1,
                  "text": "2022.06.24 -최초설정일~2022년6월26일까지:사무관리회사"}
        for text in (period["text"], "최초 설정일 ~2026년 06월18일",
                     "최초설정일부터 2022년10월16일까지", "최초설정일로부터2025년3월31일까지"):
            with self.subTest(text=text):
                self.assertIsNone(extract({"nodes": [{**period, "text": text}]})["dates"]["inception_date"])
        history = table("history", [["변경일", "내용"], ["2018.04.20", "최초설정"]])
        summary = table("summary", [["종류", "최초설정일"], ["투자신탁", "2018.04.20"]])
        for source in (history, summary, {"id": "history_text", "type": "paragraph", "page": 2,
                                         "text": "2018.04.20 최초설정"}):
            item = extract({"nodes": [period, source]})["dates"]["inception_date"]
            self.assertEqual(item["value"], "2018-04-20")
            self.assertEqual(item["node"], source["id"])
        summary = table("summary", [["종류", "최초설정일", "최근1년"],
                                    ["종류", "최초설정일", "2025.12.31"],
                                    ["투자신탁", "2018.04.20", "5"]])
        item = extract({"nodes": [period, summary]})["dates"]["inception_date"]
        self.assertEqual(item["value"], "2018-04-20")
        self.assertEqual(item["raw"], "2018.04.20")

    def test_fund_type_uses_classification_or_name_tag(self):
        tax = {"id": "tax", "type": "paragraph", "page": 1,
               "text": "증권 시장에 상장된 증권(채권 등 제외) 및 선물"}
        self.assertIsNone(extract({"nodes": [tax]})["classification"]["fund_type"])
        for label in ("주식형", "채권형", "혼합형", "재간접형", "주식혼합형", "채권혼합형", "주식-파생형"):
            row = table("class", [["분류", f"투자신탁, 증권형({label}), 개방형"]])
            item = extract({"nodes": [tax, row]})["classification"]["fund_type"]
            self.assertEqual(item["value"], f"증권({label.replace('-', '')})")
            self.assertEqual(item["node"], "class")
        result = extract({"document": {"fund_name": {"value": "예시상장지수투자신탁[주식]", "page": 1}},
                          "nodes": [tax]})
        self.assertEqual(result["classification"]["fund_type"]["value"], "증권(주식형)")

    def test_benchmark_preserves_parentheses_and_excludes_weight(self):
        node = {"id": "index", "type": "paragraph", "page": 1,
                "text": "비교지수 : NASDAQ Index(KRW 기준, 환헤지 안함) * 100%"}
        for suffix in (" * 100%", "100% ", ", 기타 설명"):
            result = extract({"nodes": [{**node, "text": "비교지수 : NASDAQ Index(KRW 기준, 환헤지 안함)" + suffix}]})
            self.assertEqual(result["classification"]["benchmark_index"]["value"],
                             "NASDAQ Index(KRW 기준, 환헤지 안함)")
        self.assertIsNone(extract({"nodes": [{**node, "text": "비교지수: NASDAQ Index(KRW 기준, 환헤지 안함"}]})[
            "classification"]["benchmark_index"])

    def test_active_outperformance_does_not_establish_leverage(self):
        node = {"id": "active", "type": "paragraph", "page": 1,
                "text": "비교지수 : NASDAQ Index(KRW 기준, 환헤지 안함). 비교지수를 초과하도록 운용"}
        self.assertIsNone(extract({"nodes": [node]})["classification"]["leverage"])

    def test_leverage_explicit_wins_over_implied(self):
        phrases = ["기초지수를 사용하여 순자산가치의 변동률을 기초지수의 변동률과 유사하도록",
                   "추적대상지수인 SAMPLE지수의 변동률과 유사하도록",
                   '일간변동률을"SAMPLE 지수(PR)"의 1배수와 유사하도록',
                   "기초지수(추적대상지수):SAMPLE100%"]
        for phrase in phrases:
            nodes = [{"id": "n", "type": "paragraph", "page": 1, "text": phrase}]
            self.assertEqual(extract({"nodes": nodes})["classification"]["leverage"]["value"], 1)
            for mult in (() if "1배수" in phrase else (2, -1)):
                explicit = {"id": "m", "type": "paragraph", "page": 2,
                            "text": f"기초지수의 일간변동률의 {mult}배수와 유사하도록 운용"}
                self.assertEqual(extract({"nodes": nodes + [explicit]})["classification"]["leverage"]["value"], mult)

    def test_numeric_cells(self):
        self.assertEqual([number(s) for s in ("1, 234", "(0.05)", "-2", "O.05%", "1l.2", "-", "hello", "0.1주")],
                         [1234, -0.05, -2, 0.05, 11.2, None, None, None])

    def test_latest_returns_and_missing(self):
        old = table("old", [["구분", "최근1년(2024.12.31)", "최근3년"],
                            ["투자신탁", "99", "88"], ["비교지수", "98", "87"]])
        new = table("new", [["구분", "최근1년(2025.12.31)", "최근3년"],
                            ["투자신탁", "0.05", "-"], ["비교지수", "-1", "-"]], 2)
        r = extract({"nodes": [old, new]})
        self.assertEqual(r["returns"]["1y"], {"value": .05, "page": 2, "node": "new", "raw": "0.05"})
        self.assertIsNone(r["returns"]["3y"])
        self.assertEqual(r["returns"]["as_of_date"]["value"], "2025-12-31")

    def test_merged_header_does_not_guess_year(self):
        t = table("a", [["구분", "최근1년차 최근2년차 24.01.01~25.01.01 23.01.01~24.01.01"],
                        ["투자신탁", "12"], ["비교지수", "10"]], section="annual")
        r = extract({"nodes": [t], "sections": [{"id": "annual", "path": ["연도별 수익률"]}]})
        self.assertIsNone(r["annual_returns"])

    def test_fee_examples_are_not_performance_or_rates(self):
        t = table("example", [["区分", "1년", "2년"],
                              ["투자신탁 총보수(피투자기구 포함)", "20", "42"]], section="fee")
        r = extract({"nodes": [t], "sections": [{"id": "fee", "path": ["보수 및 수수료", "예시"]}]})
        self.assertIsNone(r["returns"]["1y"])
        self.assertIsNone(r["fees"]["synthetic_ter"])
        merged = table("merged", [["구분", "집합투자지정참가 업자보수회사보수", "신탁업자보수"],
                                   ["투자신탁", "0.164", "0.02"]], section="fee")
        r = extract({"nodes": [merged], "sections": [{"id": "fee", "path": ["보수 및 수수료"]}]})
        self.assertIsNone(r["fees"]["ap"])

    def test_fund_financials_and_split_unit(self):
        self.assertIsNone(extract({"nodes": [{"id": "empty", "type": "table", "grid": [], "cells": []}]})["net_assets"])
        balance = table("b", [["항목", "2025.12.31", "2024.12.31"],
                              ["자산총계", "100", "80"], ["자본총계", "90", "75"]], section="f", unit="단위:백만원")
        income = table("i", [["항목", "2025.12.31", "2024.12.31"],
                             ["운용수익", "10", "5"], ["당기순이익", "9", "4"]], page=2, section="f")
        income["continued_from"] = "b"
        company = table("m", [["항목", "2026.12.31", "2025.12.31"],
                              ["자산총계", "999", "888"]], section="m")
        r = extract({"nodes": [balance, income, company], "sections": [
            {"id": "f", "path": ["집합투자기구의 재무정보", "요약재무정보"]},
            {"id": "m", "path": ["집합투자기구 관련회사", "요약재무정보"]}]})
        # 순자산만 원 단위 금액 하나로 남는다 (90 백만원 -> 90,000,000원).
        self.assertEqual(len(r["net_assets"]), 2)
        self.assertEqual(r["net_assets"][0]["amount"]["value"], 90_000_000)
        self.assertEqual(r["net_assets"][0]["period"]["value"], "2025-12-31")
        self.assertNotIn("financials", r)
        for head in ("2025.1.1-2025.12.31", "20251231"):  # '-' 기간, 붙여 쓴 날짜
            t = table("s", [["요약재무정보", head], ["자본총계", "7"]], unit="단위:원")
            self.assertEqual(extract({"nodes": [t]})["net_assets"][0]["period"]["value"], "2025-12-31")

    def test_datalake_flat_csv_and_nulls(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            doc = output / "sample"
            doc.mkdir()
            (doc / "sample.ocr_structure.json").write_text("{}", encoding="utf-8")
            record = extract({"nodes": [table("r", [["구분", "최근1년", "최근2년"],
                                                   ["투자신탁", "1", "2"]])]})
            (doc / "sample.fields.json").write_text(json.dumps(record), encoding="utf-8")
            self.assertEqual(rebuild_datalake(output), 1)
            csv_path = output / "datalake/etf_fields.csv.raw"
            self.assertTrue(csv_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["returns.1y"], "1.0")
            self.assertNotIn("returns.1y.page", rows[0])
            self.assertNotIn("returns.1y.node", rows[0])
            self.assertEqual(rows[0]["risk_grade"], "")
            line = json.loads((output / "datalake/etf_fields.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(line["returns"]["1y"], 1.0)


class PostprocessTests(unittest.TestCase):
    @staticmethod
    def cell(r, c, text, bbox, cs=1):
        return {"r": r, "c": c, "rs": 1, "cs": cs, "text": text, "lines": [text],
                "bbox": bbox, "fill": None, "_lines": [], "header": True}

    @staticmethod
    def piece(text, bbox):
        return {"text": text, "bbox": bbox, "score": .99}

    def test_coordinate_header_split_preserves_spanning_title(self):
        cells = [self.cell(0, 0, "지급비율(연간,%)", [0, 0, 100, 10], 2),
                 self.cell(1, 0, "관리 수탁 보수 보수", [0, 10, 100, 40], 2),
                 self.cell(2, 0, ".1", [0, 40, 50, 50]), self.cell(2, 1, ".2", [50, 40, 100, 50])]
        pieces = [self.piece("지급비율(연간,%)", [20, 1, 80, 9])]
        pieces += [self.piece(text, box) for text, box in [("관리", [10, 11, 30, 20]), ("보수", [10, 25, 30, 35]),
                                                        ("수탁", [60, 11, 80, 20]), ("보수", [60, 25, 80, 35])]]
        result = split_merged_headers(cells, [0, 50, 100], pieces)
        self.assertEqual(result[0]["cs"], 2)
        self.assertEqual([c["text"].replace(" ", "") for c in result if c["r"] == 1], ["관리보수", "수탁보수"])
        upper = dict(cells[0], text="지급비율 연간")
        subheaders = [self.cell(1, 0, "관리보수", [0, 10, 50, 40]),
                      self.cell(1, 1, "수탁보수", [50, 10, 100, 40])]
        self.assertEqual(split_merged_headers([upper, *subheaders, *cells[2:]], [0, 50, 100], pieces)[0]["cs"], 2)
        common = self.cell(0, 0, "분류 주식 채권", [0, 0, 100, 40], 2)
        pieces = [self.piece("분류", [40, 1, 60, 10]), self.piece("주식", [10, 25, 30, 35]),
                  self.piece("채권", [60, 25, 80, 35])]
        self.assertEqual(split_merged_headers([common, *cells[2:]], [0, 50, 100], pieces)[0], common)

    def test_stacked_joined_ocr_fragments(self):
        cells = [self.cell(0, 0, "집합투자지정참가 업자보수회사보수", [0, 0, 100, 35], 2),
                 self.cell(1, 0, ".1", [0, 35, 50, 45]), self.cell(1, 1, ".2", [50, 35, 100, 45])]
        pieces = [self.piece("집합투자지정참가", [10, 1, 90, 12]), self.piece("업자보수회사보수", [10, 20, 90, 31])]
        result = split_merged_headers(cells, [0, 50, 100], pieces)
        self.assertEqual([c["text"].replace(" ", "") for c in result[:2]], ["집합투자업자보수", "지정참가회사보수"])
        duplicated = [dict(cells[0], cs=1, bbox=[0, 0, 50, 35]),
                      dict(cells[0], c=1, cs=1, bbox=[50, 0, 100, 35]), *cells[1:]]
        self.assertEqual([c["text"] for c in split_merged_headers(duplicated, [0, 50, 100], pieces)[:2]],
                         [c["text"] for c in result[:2]])
        # A lone crossing fragment is insufficient evidence.
        self.assertEqual(split_merged_headers(cells, [0, 50, 100], pieces[:1])[0]["cs"], 2)

    def test_two_numeric_rows_and_dotted_leader(self):
        cells = [self.cell(0, c, text, [c*50, 0, (c+1)*50, 40])
                 for c, text in enumerate(("펀드 지수", "12 11", "8 … 7"))]
        pieces = [self.piece(text, [c*50+5, y, c*50+40, y+10]) for c, texts in enumerate(
            (("펀드", "지수"), ("12", "11"), ("8", "7"))) for y, text in zip((2, 24), texts)]
        pieces.append(self.piece("…………", [110, 17, 130, 19]))
        result, count = split_merged_rows(cells, 1, pieces)
        self.assertEqual(count, 2)
        self.assertEqual([c["text"] for c in result], ["펀드", "12", "8", "지수", "11", "7"])

    def test_document_date_labels_exclude_other_dates(self):
        def info(text):
            return document_info([{"type": "paragraph", "page": 1, "text": text}], [])
        self.assertEqual(info("4.작성 기준일 2024년 03월 17일")["as_of_date"]["value"], "2024년03월17일")
        self.assertEqual(info("<요약정보> 기준일:2024.03.17")["as_of_date"]["value"], "2024.03.17")
        for label in ("심사기준일", "심사 기준일", "분배금 지급기준일", "분배금 지급 기준일"):
            self.assertNotIn("as_of_date", info(label + ":2024.03.17"))

    def test_numeric_correction_after_header_reassignment(self):
        import numpy as np
        cells = [self.cell(r, c, text, [c*50, r*20, (c+1)*50, (r+1)*20])
                 for r, texts in enumerate((("항목", "금액", "금액"), ("자산", "10", "20"),
                                             ("부채", "30", "40"), ("파생상품", "O", "이")))
                 for c, text in enumerate(texts)]
        for c in cells:
            c["header"] = c["r"] == 0 or c["c"] == 0
        cells = split_merged_headers(cells, [0, 50, 100, 150], [])
        correct_single_cells(cells, np.full((100, 160, 3), 255, dtype=np.uint8), 1)
        self.assertEqual([c["text"] for c in cells[-2:]], ["0", "0"])
        self.assertEqual([c["ocr_text"] for c in cells[-2:]], ["O", "이"])

    def test_period_number_is_preserved_while_wrapped_dates_join(self):
        cells = [self.cell(r, c, text, [c*50, r*20, (c+1)*50, (r+1)*20])
                 for r, texts in enumerate((("제4기", "제3기"), ("(2025.01.01~", "(2024.01.01~"),
                                             ("2025.12.31)", "2024.12.31)")))
                 for c, text in enumerate(texts)]
        result, count = merge_wrapped_header_dates(cells, 3)
        self.assertEqual(count, 2)
        self.assertEqual([c["text"] for c in result],
                         ["제4기", "제3기", "(2025.01.01~ 2025.12.31)", "(2024.01.01~ 2024.12.31)"])

    def test_financial_subtotal_and_spaced_word_are_not_split(self):
        cells = [self.cell(0, c, "12 11", [c*50, 0, (c+1)*50, 40]) for c in range(2)]
        cells.append(dict(self.cell(0, 2, "23", [100, 0, 150, 60]), rs=2))
        pieces = [self.piece(text, [c*50+5, y, c*50+40, y+10])
                  for c in range(2) for y, text in ((2, "12"), (24, "11"))]
        result, count = split_merged_rows(cells, 2, pieces)
        self.assertEqual(count, 2)
        self.assertEqual(result, cells)
        heading = self.cell(0, 0, "금 액", [0, 0, 100, 20], 2)
        body = [self.cell(1, c, "12", [c*50, 20, (c+1)*50, 40]) for c in range(2)]
        pieces = [self.piece("금", [15, 2, 25, 12]), self.piece("액", [65, 2, 75, 12])]
        self.assertEqual(split_merged_headers([heading, *body], [0, 50, 100], pieces)[0], heading)

    def test_text_hole_gets_cell_only_where_ocr_text_sits(self):
        # 첫 열은 머리글 줄에만 칸이 있고 아래 줄은 가로선을 못 찾은 경우
        cells = [{"r": 0, "c": c, "rs": 1, "cs": 1, "bbox": [c * 50, 0, (c + 1) * 50, 20]} for c in range(2)]
        cells += [{"r": r, "c": 1, "rs": 1, "cs": 1, "bbox": [50, r * 20, 100, (r + 1) * 20]} for r in (1, 2)]
        pieces = [{"text": "판매회사 보수", "bbox": [5, 22, 45, 34], "score": .9}]
        self.assertEqual([(h["r"], h["c"]) for h in text_holes(cells, [0, 50, 100], pieces, [])], [(1, 0)])

    def test_fee_header_line_split_only_by_vocabulary(self):
        cells = [{"bbox": [x, 0, x + 50, 20]} for x in (0, 50, 100)]
        joined = {"text": "집합투자판매회사신탁업자", "bbox": [10, 2, 140, 12], "score": .9}
        self.assertEqual([p["text"] for p in split_fee_header_pieces([joined], cells)],
                         ["집합투자", "판매회사", "신탁업자"])
        # 한 이름이 두 칸에 걸친 병합 머리글과 일반 문구는 그대로 둔다.
        for text in ("판매회사", "투자위험등급"):
            piece = {"text": text, "bbox": [30, 2, 70, 12], "score": .9}
            self.assertEqual(split_fee_header_pieces([piece], cells[:2]), [piece])


class ShareClassTests(unittest.TestCase):
    """일반 펀드(다클래스) 보완 규칙. meta가 없으면 기존 동작이어야 한다."""
    FEES = [["구분", "지급비율", "지급비율", "지급비율"],
            ["구분", "집합투자업자보수", "판매회사보수", "총보수"],
            ["수수료선취-오프라인(A)", "0.40", "0.70", "1.10"],
            ["수수료후취-온라인슈퍼(S)", "0.40", "0.30", "0.70"]]

    def test_class_codes(self):
        self.assertEqual(class_code("수수료미징구- 오프라인- 퇴직연금형(C- Rp)"), "C-Rp")
        self.assertEqual(class_code("S-P 수수료미징구-온라인슈퍼"), "S-P")
        self.assertEqual(class_code("종류A 수수료선취-오프라인"), "A")
        self.assertEqual(class_code("…[채권]_S-P(수수료미징구-온라인슈퍼-개인연금)"), "S-P")
        self.assertEqual(class_code("수수료선취-오프라인(A) 수수료미징구-오프라인(C)"), "?")
        self.assertIsNone(class_code("삼성KODEX200증권상장지수투자신탁[주식]"))
        self.assertIsNone(class_code("iM에셋월드골드자(주식-재간접형)(H)"))
        self.assertEqual(class_code("거북이 90 증권 자투자신탁 1 호(주식)S-P2016-07-05"), "S-P")
        self.assertIsNone(class_code("거북이 90 증권 자투자신탁 1 호(주식)(운용)"))
        self.assertEqual(class_code("수수료후취-온라인슈퍼형(S"), "S")

    def test_benchmark_formula_and_label_on_next_line(self):
        for texts, name in ((["비교지수:(KOSPI 지수 × 95%) +(CD 91일물 × 5%)"], "KOSPI 지수"),
                            (["※ 비교지수:", "[KOSPI대형주지수]x30%+[KOSPI중형주지수]x70%"], "KOSPI대형주지수"),
                            (["(3)투자신탁의 비교지수(벤치마크)", "- KOSPI100%"], "KOSPI"),
                            (["비교지수: 해당사항 없음"], None)):
            nodes = [{"id": f"p{i}", "type": "paragraph", "page": 1, "text": t} for i, t in enumerate(texts)]
            got = extract({"nodes": nodes})["classification"]["benchmark_index"]
            self.assertEqual(got and got["value"], name)

    def test_target_class_returns_repairs(self):
        # 합쳐진 '클래스 + 비교지수' 행은 펀드 값, 빈 괄호 코드는 같은 문서의 설명으로 찾는다.
        grid = [["구분", "최근1년"], ["수수료선취-오프라인(A) 비교지수", "9"],
                ["수수료미징구-온라인슈퍼-개인연금(S-P) 비교지수", "9.5"]]
        r = extract({"nodes": [table("r", grid)]}, {"share_class": "S-P"})
        self.assertEqual((r["returns"]["1y"]["value"], r["benchmark_returns"]["1y"]), (9.5, None))
        lost = [["구분", "최근1년"], ["수수료후취-온라인슈퍼()", "7"], ["비교지수", "6"]]
        nodes = [{"id": "p", "type": "paragraph", "page": 1, "text": "수수료후취-온라인슈퍼(S)"}, table("r", lost)]
        self.assertEqual(extract({"nodes": nodes}, {"share_class": "S"})["returns"]["1y"]["value"], 7)

    def test_target_class_benchmark_and_volatility_from_own_group(self):
        # 비교지수·변동성은 받은 클래스 행 바로 아래 묶음에서 읽는다. 그 클래스 값이 '-'이면 빈칸이다.
        grid = [["구분", "최근1년", "설정일이후"],
                ["수수료미징구-오프라인(C)", "10", "8"], ["비교지수", "5", "4"], ["수익률변동성", "3", "2"],
                ["수수료후취-온라인슈퍼(S)", "11", "9"], ["비교지수", "5", "6"], ["수익률변동성", "-", "7"]]
        r = extract({"nodes": [table("r", grid)]}, {"share_class": "S"})
        self.assertEqual((r["benchmark_returns"]["since_inception"]["value"], r["volatility"]["since_inception"]["value"],
                          r["volatility"]["1y"]), (6, 7, None))

    def test_target_class_row_and_default_first_row(self):
        data = {"nodes": [table("f", self.FEES)]}
        default = extract(data)
        self.assertEqual(default["fees"]["ap"]["value"], .70)
        self.assertEqual(default["identity"]["share_class"]["value"], "A")
        chosen = extract(data, {"share_class": "s"})
        self.assertEqual((chosen["fees"]["ap"]["value"], chosen["fees"]["total"]["value"]), (.30, .70))
        self.assertEqual(chosen["identity"]["share_class"]["value"], "s")

    def test_transposed_trading_cost_rate_only(self):
        grid = [["구분", "지급비율", "지급비율"], ["구분", "C-P", "S-P"],
                ["총보수", "1.49", "0.96"], ["증권거래비용", "0.3495", "0.3472"]]
        for g in (grid, grid[:3] + [["증권거래비용", "17,605", "17,605"]]):
            t = table("f", g)
            for c in t["cells"]:
                c["col_path"] = [g[0][c["c"]], g[1][c["c"]]]
            got = extract({"nodes": [t]}, {"share_class": "S-P"})["trading_cost"]
            self.assertEqual(got and got["value"], .3472 if g is grid else None)

    def test_unlabeled_rows_are_not_used_for_a_target_class(self):
        grid = [row[:] for row in self.FEES[:3]]
        grid[2][0] = "합계"
        self.assertIsNone(extract({"nodes": [table("f", grid)]}, {"share_class": "S"})["fees"]["total"])

    def test_merged_header_split_by_position(self):
        grid = [["구분", "지급비율", "지급비율", "지급비율", "지급비율"],
                ["구분", "집합투자업자보수", "판매회사 신탁업자 보수", "판매회사 신탁업자 보수", "총보수"],
                ["종류A 수수료선취", "0.12", "0.19", "0.02", "0.35"]]
        fees = extract({"nodes": [table("f", grid)]})["fees"]
        self.assertEqual((fees["ap"]["value"], fees["trustee"]["value"]), (.19, .02))

    def test_sentence_fund_name_falls_back_to_name_field(self):
        data = {"document": {"fund_name": {"value": "은 계상된 투자신탁[당해 종류 수익증권의 상당액]"}},
                "nodes": [{"id": "p", "type": "paragraph", "page": 1,
                           "text": "집합투자기구 명칭: 미래에셋솔로몬장기국공채증권자투자신탁1호(채권)"}]}
        self.assertEqual(extract(data)["identity"]["fund_name"]["value"], "미래에셋솔로몬장기국공채증권자투자신탁1호(채권)")

    def test_returns_from_target_class_row(self):
        grid = [["구분", "최근1년"], ["투자신탁", "10"], ["수수료선취-오프라인(A)", "9"],
                ["수수료후취-온라인슈퍼(S)", "9.5"], ["비교지수", "8"]]
        data = {"nodes": [table("r", grid)]}
        self.assertEqual(extract(data)["returns"]["1y"]["value"], 10)
        r = extract(data, {"share_class": "S"})
        self.assertEqual((r["returns"]["1y"]["value"], r["benchmark_returns"]["1y"]["value"]), (9.5, 8))


if __name__ == "__main__":
    unittest.main()
