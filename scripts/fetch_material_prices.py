"""抓取生意社价格并写库，连接配置读取项目根目录 .env。

默认抓取含今天在内的近 7 天、init_data.sql 中的全部材料：
uv run python scripts\\fetch_material_prices.py
指定区间（含首尾日期）和材料 code 或名称：
uv run python scripts\\fetch_material_prices.py --start-date 2026-11-01 --end-date 2026-11-04 --materials copper 铝

source_id 沿用项目约定为 1。免费接口仅提供近一年，区间在本地过滤；
无报价的日期跳过，不写 NULL、不补齐、不删除旧价格。
"""

import argparse
import ast
import asyncio
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
from time import sleep
import urllib.request

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import create_database_engine
from models import Material, MaterialPrice


# 名称采用生意社图表标题，铁矿石使用澳大利亚口径，型钢使用 H型钢。
PRODUCTS = {
    "copper": (524, "铜"),
    "aluminum": (482, "铝"),
    "lead": (379, "铅"),
    "zinc": (431, "锌"),
    "nickel": (432, "镍"),
    "tin": (492, "锡"),
    "rebar": (927, "螺纹钢"),
    "hot_rolled_coil": (195, "热轧板卷"),
    "wire_rod": (740, "线材"),
    "iron_ore": (961, "铁矿石(澳)"),
    "stainless_steel": (1300, "不锈钢"),
    "section_steel": (1172, "H型钢"),
}
SOURCE_ID = 1
YEAR_URL = (
    "https://www.100ppi.com/graph/cindex.php"
    "?f=graph_ave&ppid={ppid}&type=1&mode=2"
)
ALPHA = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def fetch(url, referer="https://hxg.100ppi.com/"):
    """按指南处理 HW_CHECK；保留默认 TLS 证书校验。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    for _ in range(3):
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode("utf-8")
        challenge = re.search(r'var\s+_0x2\s*=\s*"([0-9a-fA-F]+)"', text)
        if challenge:
            headers["Cookie"] = f"HW_CHECK={challenge.group(1)}"
            sleep(0.8)
            continue
        if "安全检查" in text:
            raise RuntimeError("无法解析 HW_CHECK 校验页")
        return text
    raise RuntimeError("HW_CHECK 校验未通过")


def unpack(text):
    """解包指南中的 Dean Edwards packer，不执行远端 JavaScript。"""
    match = re.search(
        r"eval\(function\(p,a,c,k,e,d\).*?\}\('(.*?)',(\d+),(\d+),"
        r"'(.*?)'\.split\('\|'\)",
        text,
        re.S,
    )
    if not match:
        if "eval(function(" in text:
            raise ValueError("无法识别图表混淆格式")
        return text
    payload, base = match.group(1), int(match.group(2))
    if not 2 <= base <= len(ALPHA):
        raise ValueError(f"不支持的 packer 进制：{base}")
    words = [word.replace("\\'", "'") for word in match.group(4).split("|")]
    payload = payload.replace("\\'", "'").replace("\\\\", "\\")

    def replace(token):
        index = 0
        for char in token.group():
            digit = ALPHA.index(char)
            if digit >= base:
                return token.group()
            index = index * base + digit
        return words[index] if index < len(words) and words[index] else token.group()

    return re.sub(r"\b[0-9a-zA-Z]+\b", replace, payload)


def parse_chart(text, expected_title):
    """返回日期价格映射及口径；空图表有效，畸形图表报错。"""
    text = unpack(text)
    title = re.search(r"(?<!\w)text:\s*'([^']+)'", text)
    if not title or title.group(1) != expected_title:
        raise ValueError(f"响应不是 {expected_title} 图表")
    axis = re.search(r"xAxis:\s*\{[^}]*data:\s*(\[[^\]]*\])", text, re.S)
    series = re.search(r"series:\s*\[\s*(\{.*?\}\s*,?\s*)?\]", text, re.S)
    if not axis or not series:
        raise ValueError("图表缺少日期轴或价格序列")
    dates = ast.literal_eval(axis.group(1))
    if series.group(1):
        data = re.search(r"\bdata:\s*\[([^\]]*)\]", series.group(1), re.S)
        if not data:
            raise ValueError("价格序列中没有 data 数组")
        raw_values = data.group(1).strip().removesuffix(",").strip()
        values = raw_values.split(",") if raw_values else []
    else:
        values = []
    if len(dates) != len(values):
        raise ValueError("日期和价格数量不一致，拒绝错位写入")

    prices = {}
    for day, value in zip(dates, values):
        parsed_day = date.fromisoformat(day)
        if parsed_day.isoformat() != day or parsed_day in prices:
            raise ValueError(f"日期格式错误或重复：{day}")
        value = value.strip()
        if value in ("null", "''", '""', "'-'", '"-"'):
            prices[parsed_day] = None
            continue
        try:
            price = Decimal(value)
        except InvalidOperation as error:
            raise ValueError(f"无效价格：{value}") from error
        if (
            not price.is_finite()
            or not 0 < price <= Decimal("99999999.99")
            or price != price.quantize(Decimal("0.01"))
        ):
            raise ValueError(f"价格不符合 decimal(10,2)：{value}")
        prices[parsed_day] = price
    subtext = re.search(r"subtext:\s*'([^']*)'", text)
    return prices, subtext.group(1) if subtext else "接口未提供地区、规格等详细口径"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=date.fromisoformat, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--materials", nargs="+", help="材料 code 或名称，空格分隔")
    args = parser.parse_args(argv)
    args.end_date = args.end_date or date.today()
    args.start_date = args.start_date or args.end_date - timedelta(days=6)
    if args.start_date > args.end_date:
        parser.error("开始日期不能晚于结束日期")
    if args.end_date == date.max:
        parser.error("结束日期必须早于 9999-12-31")
    return args


def select_materials(materials, requested=None):
    if requested is None:
        selected = [material for material in materials if material.code in PRODUCTS]
    else:
        selected = []
        for token in requested:
            matches = [
                material for material in materials
                if token in (material.code, material.name)
            ]
            if len(matches) != 1:
                raise ValueError(f"材料不存在或名称不唯一：{token}，请使用 material.code")
            material = matches[0]
            if material.code not in PRODUCTS:
                raise ValueError(f"材料尚未配置生意社 ppid：{material.code}")
            if material not in selected:
                selected.append(material)
    return sorted(selected, key=lambda material: material.id)


def prices_in_range(text, code, start, end):
    _, name = PRODUCTS[code]
    prices, subtext = parse_chart(text, expected_title=f"{name}生意社价格")
    return {
        day: price for day, price in prices.items()
        if start <= day <= end and price is not None
    }, subtext


async def upsert_prices(session, material_id, prices):
    if not prices:
        return 0, 0
    # 表无日期唯一键；锁住材料行，使本脚本的并发运行串行检查、写入。
    (await session.execute(
        select(Material.id).where(Material.id == material_id).with_for_update()
    )).scalar_one()
    existing = (await session.execute(
        select(MaterialPrice).where(
            MaterialPrice.material_id == material_id,
            MaterialPrice.source_id == SOURCE_ID,
            MaterialPrice.stat_date >= datetime.combine(min(prices), time.min),
            MaterialPrice.stat_date < datetime.combine(max(prices) + timedelta(days=1), time.min),
        ).with_for_update()
    )).scalars().all()
    by_day = {}
    for record in existing:
        by_day.setdefault(record.stat_date.date(), []).append(record)

    inserted = updated = 0
    now = datetime.now()
    for day, price in sorted(prices.items()):
        records = by_day.get(day, [])
        if records:
            for record in records:
                record.price = price
                record.updated_at = now
            updated += len(records)
        else:
            session.add(MaterialPrice(
                material_id=material_id,
                source_id=SOURCE_ID,
                price=price,
                stat_date=datetime.combine(day, time.min),
            ))
            inserted += 1
    return inserted, updated


async def main(args):
    engine = create_database_engine()
    try:
        async with AsyncSession(engine) as session:
            materials = (await session.execute(select(Material))).scalars().all()
            selected = select_materials(materials, args.materials)
        if not selected:
            print("material 表中没有匹配的初始材料，跳过。")
            return

        print(f"抓取区间：{args.start_date} ~ {args.end_date}；来源 ID：{SOURCE_ID}")
        batches = []
        for index, material in enumerate(selected):
            if index:
                await asyncio.sleep(0.8)
            ppid, _ = PRODUCTS[material.code]
            text = await asyncio.to_thread(
                fetch, YEAR_URL.format(ppid=ppid),
                referer=f"https://www.100ppi.com/rawmex/detail-{ppid}.html",
            )
            prices, subtext = prices_in_range(
                text, material.code, args.start_date, args.end_date
            )
            print(f"{material.name}（{material.code}）：有效日期 {len(prices)}；口径：{subtext}")
            batches.append((material, prices))

        # 全部响应通过校验后才写库，任一写入失败时整批回滚。
        results = []
        async with AsyncSession(engine) as session, session.begin():
            for material, prices in batches:
                inserted, updated = await upsert_prices(session, material.id, prices)
                results.append((material.code, inserted, updated))
        for code, inserted, updated in results:
            print(f"{code}：插入 {inserted} 条，更新 {updated} 条"
                  + ("，区间内无有效报价，跳过" if not inserted and not updated else ""))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
