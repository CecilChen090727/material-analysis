from datetime import date, datetime
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from fetch_material_prices import (
    Material, MaterialPrice, fetch, parse_args, prices_in_range,
    select_materials, unpack, upsert_prices,
)
from models import Base


class MaterialPriceTests(unittest.TestCase):
    def test_fetch_retries_check_cookie_and_preserves_referer(self):
        with (
            patch("fetch_material_prices.urllib.request.urlopen") as urlopen,
            patch("fetch_material_prices.sleep") as sleep,
        ):
            urlopen.return_value.__enter__.return_value.read.side_effect = [
                b'var _0x2 = "abcdef";', b"chart",
            ]
            self.assertEqual(fetch("https://www.100ppi.com/", referer="https://hrb.100ppi.com/"), "chart")
            request = urlopen.call_args.args[0]
            self.assertEqual(request.get_header("Cookie"), "HW_CHECK=abcdef")
            self.assertEqual(request.get_header("Referer"), "https://hrb.100ppi.com/")
            sleep.assert_called_once_with(0.8)

    def test_packer_uses_base62_and_zero_based_word_indices(self):
        words = [""] * 38
        words[10], words[36], words[37] = "lower", "upper", "last"
        text = (
            "eval(function(p,a,c,k,e,d){}('a A B',62,38,'"
            + "|".join(words)
            + "'.split('|'),0,{}))"
        )
        self.assertEqual(unpack(text), "lower upper last")

    def test_cli_defaults_and_explicit_range(self):
        args = parse_args([])
        self.assertEqual(args.end_date, date.today())
        self.assertEqual((args.end_date - args.start_date).days, 6)
        args = parse_args(["--end-date", "2026-11-04", "--materials", "copper", "铝"])
        self.assertEqual(args.start_date, date(2026, 10, 29))
        self.assertEqual(args.materials, ["copper", "铝"])
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parse_args(["--start-date", "2026-11-05", "--end-date", "2026-11-04"])

    def test_selects_seed_materials_or_explicit_codes_and_names(self):
        copper = Material(id=1, code="copper", name="铜")
        steel = Material(id=12, code="section_steel", name="型钢")
        other = Material(id=13, code="other", name="其他")
        materials = [other, steel, copper]
        self.assertEqual(select_materials(materials), [copper, steel])
        self.assertEqual(select_materials(materials, ["铜", "copper"]), [copper])
        for tokens in (["missing"], ["other"]):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                select_materials(materials, tokens)

    def test_filters_dates_and_missing_prices_without_losing_alignment(self):
        text = (
            "title:{text:'铜生意社价格'},"
            "xAxis:{data:['2026-10-31','2026-11-01','2026-11-02','2026-11-03']},"
            "series:[{data:[100,null,102.50,103]}]"
        )
        prices, _ = prices_in_range(text, "copper", date(2026, 11, 1), date(2026, 11, 2))
        self.assertEqual(prices, {date(2026, 11, 2): Decimal("102.50")})
        prices, _ = prices_in_range(text, "copper", date(2020, 1, 1), date(2020, 1, 7))
        self.assertEqual(prices, {})
        with self.assertRaises(ValueError):
            prices_in_range(text, "aluminum", date(2026, 11, 1), date(2026, 11, 2))

    def test_empty_section_steel_is_skipped(self):
        prices, _ = prices_in_range(
            "title:{text:'H型钢生意社价格'},xAxis:{data:[]},series:[]",
            "section_steel", date(2026, 11, 1), date(2026, 11, 4),
        )
        self.assertEqual(prices, {})

    def test_real_chart_nested_styles_and_trailing_commas(self):
        text = (
            "title:{text:'热轧板卷生意社价格'},"
            "xAxis:{type:'category',data:['2026-11-03',]},"
            "series:[{name:'价格',type:'line',lineStyle:{normal:{}},"
            "data:[3500.25,]},]"
        )
        prices, _ = prices_in_range(
            text, "hot_rolled_coil", date(2026, 11, 1), date(2026, 11, 4)
        )
        self.assertEqual(prices, {date(2026, 11, 3): Decimal("3500.25")})
        prices, _ = prices_in_range(
            text.replace("热轧板卷", "铁矿石(澳)"),
            "iron_ore", date(2026, 11, 1), date(2026, 11, 4),
        )
        self.assertEqual(prices, {date(2026, 11, 3): Decimal("3500.25")})


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.session.add_all([
            Material(id=1, code="copper", name="铜"),
            Material(id=2, code="aluminum", name="铝"),
        ])
        self.session.commit()
        # 用真实 SQLite ORM 会话执行相同 SQL，仅将 execute 适配成异步接口。
        self.adapter = AsyncMock()
        self.adapter.execute.side_effect = self.session.execute
        self.adapter.add = self.session.add

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    async def test_updates_existing_duplicates_inserts_missing_and_preserves_others(self):
        def record(material_id, source_id, day, price):
            return MaterialPrice(
                material_id=material_id, source_id=source_id, price=Decimal(price),
                stat_date=datetime.fromisoformat(day),
                updated_at=datetime(2020, 1, 1),
            )

        original = [
            record(1, 1, "2026-11-01", "10"),
            record(1, 1, "2026-11-01T12:00:00", "11"),
            record(1, 2, "2026-11-01", "20"),
            record(2, 1, "2026-11-01", "30"),
            record(1, 1, "2026-11-03", "40"),
            record(1, 1, "2026-10-31", "50"),
        ]
        self.session.add_all(original)
        self.session.commit()
        prices = {date(2026, 11, 1): Decimal("100"), date(2026, 11, 2): Decimal("200")}
        self.assertEqual(await upsert_prices(self.adapter, 1, prices), (1, 2))
        self.session.commit()
        self.assertEqual([row.price for row in original],
                         list(map(Decimal, ["100", "100", "20", "30", "40", "50"])))
        self.assertGreater(original[0].updated_at, datetime(2020, 1, 1))
        self.assertEqual(original[2].updated_at, datetime(2020, 1, 1))
        rows = self.session.scalars(select(MaterialPrice)).all()
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[-1].stat_date, datetime(2026, 11, 2))
        self.assertEqual(await upsert_prices(self.adapter, 1, prices), (0, 3))
        self.session.commit()
        self.assertEqual(len(self.session.scalars(select(MaterialPrice)).all()), 7)

    async def test_empty_prices_never_touch_database(self):
        self.assertEqual(await upsert_prices(self.adapter, 1, {}), (0, 0))
        self.adapter.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
