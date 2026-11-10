"""Small offline checks for extraction's highest-risk decisions."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from extract_fields import extract, number, rebuild_datalake


def table(ident, grid, page=1, section=None, unit=None):
    return {"id": ident, "type": "table", "page": page, "section": section,
            "n_cols": len(grid[0]), "grid": grid, "unit": unit,
            "cells": [{"r": r, "c": c, "rs": 1, "cs": 1, "text": text}
                      for r, row in enumerate(grid) for c, text in enumerate(row)]}


class ExtractionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
