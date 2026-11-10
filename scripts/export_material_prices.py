"""导出来源为 1 的每日材料价格，缺失价格留空。

在项目根目录 .env 中设置 DATABASE_URL（参见 .env.example），然后运行：
uv run python scripts\\export_material_prices.py

输出：excel\\YYYYMMDD_material_prices.xlsx（日期为生成当天的年月日）
同一材料同一天有多条记录时拒绝导出，不自动求平均或覆盖。
"""

import asyncio
from pathlib import Path
import sys

from openpyxl import Workbook
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import create_database_engine
from models import Material, MaterialPrice


OUTPUT = ROOT / "excel" / "material_prices.xlsx"


def price_query():
    return (
        select(Material.id, Material.name, MaterialPrice.stat_date, MaterialPrice.price)
        .outerjoin(
            MaterialPrice,
            and_(
                MaterialPrice.material_id == Material.id,
                MaterialPrice.source_id == 1,
            ),
        )
        .order_by(Material.id, MaterialPrice.stat_date, MaterialPrice.id)
    )


def write_excel(records, output=OUTPUT):
    materials = {}
    prices = {}
    for material_id, name, stat_date, price in records:
        materials[material_id] = name
        if stat_date is None:
            continue
        day = stat_date.date()
        daily_prices = prices.setdefault(day, {})
        if material_id in daily_prices:
            raise ValueError(f"{day} 的材料 {name}（ID={material_id}）存在多条价格")
        daily_prices[material_id] = price

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "材料价格"
    sheet.append(["日期", *materials.values()])
    for cell in sheet[1]:
        cell.data_type = "s"
    for day, daily_prices in sorted(prices.items()):
        sheet.append([day, *(daily_prices.get(key) for key in materials)])
        sheet.cell(sheet.max_row, 1).number_format = "yyyy-mm-dd"
        for cell in sheet[sheet.max_row][1:]:
            cell.number_format = "0.00"
    sheet.freeze_panes = "B2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.column_dimensions["A"].width = 14
    for column in range(2, len(materials) + 2):
        sheet.column_dimensions[sheet.cell(1, column).column_letter].width = 18
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)
    workbook.close()
    return len(prices), len(materials)


async def main():
    engine = create_database_engine()
    try:
        async with AsyncSession(engine) as session:
            records = (await session.execute(price_query())).all()
        days, materials = write_excel(records)
        print(f"已生成：{OUTPUT}，共 {days} 个日期、{materials} 种材料")
    finally:
        await engine.dispose()



if __name__ == "__main__":
    asyncio.run(main())
