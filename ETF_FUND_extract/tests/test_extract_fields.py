"""Small offline checks for extraction's highest-risk decisions."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extract_fields import extract, number, rebuild_datalake, fee_key
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ETF_FUND_postprocess"))
from build_doc_structure import document_info
from build_doc_structure_ocr import (split_merged_headers, split_merged_rows,
                                     merge_wrapped_header_dates, correct_single_cells)


def table(ident, grid, page=1, section=None, unit=None):
    return {"id": ident, "type": "table", "page": page, "section": section,
            "n_cols": len(grid[0]), "grid": grid, "unit": unit,
            "cells": [{"r": r, "c": c, "rs": 1, "cs": 1, "text": text}
                      for r, row in enumerate(grid) for c, text in enumerate(row)]}


class ExtractionTests(unittest.TestCase):
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
        for label in ("주식형", "채권형", "혼합형", "재간접형", "주식혼합형", "채권혼합형"):
            row = table("class", [["분류", f"투자신탁, 증권형({label}), 개방형"]])
            item = extract({"nodes": [tax, row]})["classification"]["fund_type"]
            self.assertEqual(item["value"], f"증권({label})")
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
            self.assertEqual(rows[0]["returns.1y.page"], "1")
            self.assertNotIn("returns.1y.node", rows[0])
            self.assertEqual(rows[0]["risk_grade"], "")


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


if __name__ == "__main__":
    unittest.main()
