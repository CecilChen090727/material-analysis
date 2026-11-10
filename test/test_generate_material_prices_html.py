from datetime import date, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import Workbook

from generate_material_prices_html import build_payload, generate_html, read_prices


class GenerateMaterialPricesHtmlTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / "prices.xlsx"
        self.output = Path(directory.name) / "prices.html"

    def write_workbook(self, rows):
        workbook = Workbook()
        for row in rows:
            workbook.active.append(row)
        workbook.save(self.source)
        workbook.close()

    def test_calendar_year_boundaries_and_future_dates(self):
        self.write_workbook([
            ["日期", "铜"],
            [datetime(2026, 11, 5), 500],
            [datetime(2025, 11, 3), 100],
            [datetime(2026, 11, 4), 400],
            [datetime(2025, 11, 4), 200],
        ])
        names, daily = read_prices(self.source, date(2026, 11, 4))
        self.assertEqual(names, ["铜"])
        self.assertEqual(daily, {
            date(2025, 11, 3): [100],
            date(2025, 11, 4): [200],
            date(2026, 11, 4): [400],
        })
        payload = build_payload(names, daily, datetime(2026, 11, 4), {})
        self.assertEqual(payload["series"]["铜"], [
            {"d": "2025-11-04", "v": 200}, {"d": "2026-11-04", "v": 400},
        ])
        self.assertEqual(payload["metrics"]["铜"]["chg_year_pct"], 100)

    def test_leap_day_uses_previous_february_28(self):
        self.write_workbook([
            ["日期", "铜"],
            [datetime(2023, 2, 27), 10],
            [datetime(2023, 2, 28), 20],
            [datetime(2024, 2, 29), 30],
        ])
        names, daily = read_prices(self.source, date(2024, 2, 29))
        payload = build_payload(names, daily, datetime(2024, 2, 29), {})
        self.assertEqual(payload["series"]["铜"], [
            {"d": "2023-02-28", "v": 20}, {"d": "2024-02-29", "v": 30},
        ])
        self.assertEqual(payload["metrics"]["铜"]["chg_year_pct"], 50)

    def test_year_baseline_outside_display_window_uses_full_history(self):
        self.write_workbook([
            ["日期", "铜", "铝"],
            [datetime(2025, 11, 2), 100, 50],
            [datetime(2025, 11, 3), 120, None],
            [datetime(2025, 11, 4), 500, 700],
            [datetime(2026, 11, 3), 180, 75],
        ])
        generate_html(self.source, self.output, datetime(2026, 11, 4))
        page = self.output.read_text(encoding="utf-8")
        start = page.index("    const PAY = ") + len("    const PAY = ")
        end = page.index("    const D = PAY.series", start)
        payload = json.loads(page[start:end].strip().removesuffix(";"))
        self.assertEqual(payload["meta"]["range"], ["2025-11-04", "2026-11-03"])
        self.assertEqual(payload["meta"]["days"], 2)
        self.assertEqual(payload["series"]["铜"], [
            {"d": "2025-11-04", "v": 500}, {"d": "2026-11-03", "v": 180},
        ])
        copper = payload["metrics"]["铜"]
        self.assertEqual(copper["prev_year_date"], "2025-11-03")
        self.assertEqual(copper["chg_year"], 60)
        self.assertEqual(copper["chg_year_pct"], 50)
        self.assertEqual((copper["lo_1y"], copper["hi_1y"]), (180, 500))
        aluminum = payload["metrics"]["铝"]
        self.assertEqual(aluminum["prev_year_date"], "2025-11-02")
        self.assertEqual(aluminum["chg_year_pct"], 50)

    def test_short_history_missing_prices_and_zero_baseline(self):
        self.write_workbook([
            ["日期", "铜", "新材料", "无报价"],
            [datetime(2026, 10, 1), 0, None, None],
            [datetime(2026, 11, 1), None, 50, None],
            [datetime(2026, 11, 4), 100, 60, None],
        ])
        names, daily = read_prices(self.source, date(2026, 11, 4))
        payload = build_payload(names, daily, datetime(2026, 11, 4), {})
        self.assertEqual(len(payload["series"]["铜"]), 3)
        self.assertIsNone(payload["series"]["铜"][1]["v"])
        self.assertIsNone(payload["metrics"]["铜"]["chg_month_pct"])
        self.assertEqual(payload["metrics"]["铜"]["chg_day"], 100)
        self.assertEqual(payload["metrics"]["新材料"]["chg_day_pct"], 20)
        self.assertIsNone(payload["metrics"]["新材料"]["chg_year_pct"])
        self.assertIsNone(payload["metrics"]["无报价"]["cur"])

    def test_generates_offline_template_with_safe_names_and_fixed_timestamp(self):
        name = """新材料'</script><b>"""
        self.write_workbook([
            ["日期", name],
            [datetime(2026, 11, 1), 123.45],
        ])
        self.assertEqual(
            generate_html(self.source, self.output, datetime(2026, 11, 4, 14, 12)),
            self.output,
        )
        page = self.output.read_text(encoding="utf-8")
        start = page.index("    const PAY = ") + len("    const PAY = ")
        end = page.index("    const D = PAY.series", start)
        payload = json.loads(page[start:end].strip().removesuffix(";"))
        self.assertEqual(payload["com"], [name])
        self.assertEqual(payload["meta"]["generated_at"], "2026-11-04 14:12:00")
        self.assertEqual(payload["meta"]["range"], ["2026-11-01", "2026-11-01"])
        self.assertNotIn(name, page)
        self.assertNotIn('<script src=', page)
        self.assertIn('echarts.init', page)
        self.assertIn('onclick="pick(PAY.com[${COM.indexOf(c)}])"', page)

    def test_empty_recent_window_does_not_overwrite_output(self):
        self.write_workbook([["日期", "铜"], [datetime(2020, 1, 1), 100]])
        self.output.write_text("existing", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "近一年内没有数据"):
            generate_html(self.source, self.output, datetime(2026, 11, 4))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "existing")

    def test_duplicate_dates_are_rejected(self):
        self.write_workbook([
            ["日期", "铜"],
            [datetime(2026, 11, 4), 100],
            [datetime(2026, 11, 4), 200],
        ])
        with self.assertRaisesRegex(ValueError, "重复日期"):
            read_prices(self.source, date(2026, 11, 4))


if __name__ == "__main__":
    unittest.main()
