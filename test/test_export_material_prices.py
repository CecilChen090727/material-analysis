from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from export_material_prices import price_query, write_excel
from models import Base, Material, MaterialPrice


class ExportMaterialPricesTests(unittest.TestCase):
    def test_join_filters_source_and_exports_all_materials_by_date(self):
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all([
                Material(id=1, code="copper", name="铜"),
                Material(id=2, code="aluminum", name="铝"),
                Material(id=3, code="lead", name="铅"),
                Material(id=4, code="zinc", name="=1+1"),
                MaterialPrice(
                    material_id=1, source_id=1, price=Decimal("123.45"),
                    stat_date=datetime(2026, 11, 4, 12),
                ),
                MaterialPrice(
                    material_id=1, source_id=1, price=None,
                    stat_date=datetime(2026, 11, 3),
                ),
                MaterialPrice(
                    material_id=2, source_id=1, price=Decimal("0.00"),
                    stat_date=datetime(2026, 11, 3),
                ),
                MaterialPrice(
                    material_id=3, source_id=2, price=Decimal("999.00"),
                    stat_date=datetime(2026, 11, 2),
                ),
            ])
            session.commit()
            records = session.execute(price_query()).all()

        with TemporaryDirectory() as directory:
            output = Path(directory) / "excel" / "prices.xlsx"
            self.assertEqual(write_excel(records, output), (2, 4))
            workbook = load_workbook(output)
            self.addCleanup(workbook.close)
            sheet = workbook.active
            self.assertEqual(list(sheet.values), [
                ("日期", "铜", "铝", "铅", "=1+1"),
                (datetime(2026, 11, 3), None, 0, None, None),
                (datetime(2026, 11, 4), 123.45, None, None, None),
            ])
            self.assertEqual(sheet["E1"].data_type, "s")
            self.assertEqual(sheet["A2"].number_format, "yyyy-mm-dd")
            self.assertEqual(sheet["B3"].number_format, "0.00")

    def test_no_prices_still_exports_all_material_headers(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "prices.xlsx"
            self.assertEqual(write_excel([(1, "铜", None, None)], output), (0, 1))
            workbook = load_workbook(output)
            self.addCleanup(workbook.close)
            self.assertEqual(list(workbook.active.values), [("日期", "铜")])

    def test_duplicate_daily_prices_fail_without_overwriting_output(self):
        records = [
            (1, "铜", datetime(2026, 11, 4), None),
            (1, "铜", datetime(2026, 11, 4, 12), Decimal("123.45")),
        ]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "prices.xlsx"
            output.write_bytes(b"existing report")
            with self.assertRaisesRegex(ValueError, "存在多条价格"):
                write_excel(records, output)
            self.assertEqual(output.read_bytes(), b"existing report")


if __name__ == "__main__":
    unittest.main()
