"""根据 Excel 生成可离线查看的材料价格看板。

运行：uv run python scripts\\generate_material_prices_html.py
输入：excel\\material_prices_20261104.xlsx
输出：html\\YYYYMMDD_material_prices.html
可通过 --input 和 --output 指定文件，不会覆盖参考 HTML。
图表只展示生成当天（含）向前一个日历年的数据，缺失价格不补值。
涨跌指标使用 Excel 完整历史中的基准报价，不受图表展示区间限制。
"""

import argparse
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "excel" / "material_prices.xlsx"
TEMPLATE = ROOT / "html" / "大宗商品现货价格追踪.html"


def previous_year(day):
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def read_prices(source, today):
    workbook = load_workbook(source, read_only=True, data_only=True)
    try:
        rows = workbook.active.iter_rows(values_only=True)
        header = next(rows, ())
        if len(header) < 2 or header[0] != "日期":
            raise ValueError("Excel 首列必须为“日期”，其余列为材料名称")
        names = list(header[1:])
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("材料名称不能为空")
        if len(set(names)) != len(names):
            raise ValueError("材料名称不能重复")

        cutoff = previous_year(today)
        daily = {}
        for row_number, row in enumerate(rows, 2):
            if all(value is None for value in row):
                continue
            day = row[0]
            if isinstance(day, datetime):
                day = day.date()
            elif isinstance(day, str):
                day = date.fromisoformat(day)
            if not isinstance(day, date):
                raise ValueError(f"第 {row_number} 行日期无效")
            if day > today:
                continue
            if day in daily:
                raise ValueError(f"{day} 存在重复日期")
            values = []
            for name, value in zip(names, row[1:]):
                if value is not None:
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                    ):
                        raise ValueError(f"第 {row_number} 行材料 {name} 的价格无效")
                values.append(value)
            daily[day] = values
        if not any(day >= cutoff for day in daily):
            raise ValueError("生成日前近一年内没有数据")
        return names, dict(sorted(daily.items()))
    finally:
        workbook.close()


def build_payload(names, daily, generated_at, groups):
    cutoff = previous_year(generated_at.date())
    visible = {day: values for day, values in daily.items() if cutoff <= day <= generated_at.date()}
    days = list(visible)
    series = {}
    metrics = {}
    for column, name in enumerate(names):
        points = [{"d": day.isoformat(), "v": values[column]} for day, values in visible.items()]
        series[name] = points
        history = [
            (day, values[column]) for day, values in daily.items()
            if day <= generated_at.date() and values[column] is not None
        ]
        quoted = [(day, value) for day, value in history if day >= cutoff]
        latest_day, current = quoted[-1] if quoted else (None, None)
        values = [value for _, value in quoted]
        metric = {
            "cur": current,
            "cur_date": latest_day.isoformat() if latest_day else "暂无报价",
            "lo_1y": min(values) if values else None,
            "hi_1y": max(values) if values else None,
        }
        baselines = {
            "day": None,
            "month": None,
            "year": None,
        }
        if latest_day:
            baselines["day"] = next(
                (point for point in reversed(history) if point[0] < latest_day), None
            )
            for period, target in (
                ("month", latest_day - timedelta(days=30)),
                ("year", previous_year(latest_day)),
            ):
                baselines[period] = next(
                    (point for point in reversed(history) if point[0] <= target), None
                )
        for period, baseline in baselines.items():
            difference = current - baseline[1] if baseline else None
            metric[f"prev_{period}_date"] = baseline[0].isoformat() if baseline else None
            metric[f"chg_{period}"] = difference
            metric[f"chg_{period}_pct"] = (
                difference / baseline[1] * 100 if baseline and baseline[1] != 0 else None
            )
        metrics[name] = metric
    return {
        "meta": {
            "range": [days[0].isoformat(), days[-1].isoformat()],
            "days": len(days),
            "latest": max(
                (point["d"] for points in series.values() for point in points if point["v"] is not None),
                default="暂无报价",
            ),
            "generated_at": generated_at.strftime("%Y-%m-%d %H:%M:%S"),
            "today": generated_at.date().isoformat(),
        },
        "com": names,
        "group": {name: groups.get(name, "其他") for name in names},
        "series": series,
        "metrics": metrics,
    }


def render_html(template, payload):
    start = template.index("    const PAY = ")
    end = template.index("    const D = PAY.series", start)
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    page = template[:start] + f"    const PAY = {data};\n" + template[end:]
    replacements = [
        (
            "数据来源：<b>生意社 100ppi.com</b>（全国现货均价）",
            '数据来源：<b><a href="https://www.100ppi.com/" style="color:inherit">https://www.100ppi.com/</a></b>',
        ),
        ("交易日 <b", "数据日 <b"),
        ("点击表头行可切换主图品种", "点击材料行可切换主图品种"),
        ("以区间起点为 100 归一化，点击图例可增删品种",
         "以各品种区间首个非零报价为基准计算涨跌幅，点击图例可增删品种"),
        (
            "本看板使用 <code>生意社(100ppi.com)</code> 发布的<b>全国现货均价</b>，与含地区明细的付费源（Mysteel/SMM/长江有色）口径不同，数值不可直接互换。",
            "本看板读取 Excel 中的原始价格，图表仅展示生成当天向前一个日历年的数据（含边界）；不足一年则全部展示，不包含未来日期。",
        ),
        (
            "环比 = 与 30 天前后最近交易日相比；同比 = 与 365 天前后最近交易日相比；涨跌 = 现价 − 基准价。",
            "环比 = 与最新报价日 30 天前当日或之前的最近报价相比；同比 = 与上一年同日或之前的最近报价相比；指标基准从 Excel 完整历史中获取，无可用基准时显示“—”。",
        ),
        ("数据仅含交易日，法定节假日无报价。", "缺失价格留空，不插值；零基准不计算涨跌幅。"),
        ('$("#gen").textContent = new Date().toLocaleString("zh-CN");',
         '$("#gen").textContent = PAY.meta.generated_at;'),
        ('let cur = "铜", range = 365;', 'let cur = COM[0], range = 99999;'),
        (
            'const RANGES = [["近1月", 30], ["近3月", 90], ["近6月", 180], ["近1年", 365], ["近2年", 730], ["全部", 99999]];',
            'const RANGES = [["近1月", 30], ["近3月", 90], ["近6月", 180], ["近1年", 99999]];',
        ),
        ('dy === 365', 'dy === 99999'),
        ('const end = new Date(s[s.length - 1].d);', 'const end = new Date(PAY.meta.today);'),
        (
            '${((m.hi_1y - m.lo_1y) / m.lo_1y * 100).toFixed(1)}%',
            '${m.lo_1y ? ((m.hi_1y - m.lo_1y) / m.lo_1y * 100).toFixed(1) + "%" : "—"}',
        ),
        ('const base = s[0].v;', 'const base = s.find(x => x.v != null && x.v !== 0)?.v;\n'
         '            if (base == null) return null;'),
        ('data: s.map(x => +((x.v / base - 1) * 100).toFixed(2))',
         'data: s.map(x => x.v == null ? null : +((x.v / base - 1) * 100).toFixed(2))'),
        ('const pm = i > 0 ? s[i - 1].v : null;',
         'const pm = s.slice(0, i).findLast(y => y.v != null)?.v;'),
        ('(pm ? `<br/>较前一日：', '(pm && x.data != null ? `<br/>较前一日：'),
        ('${cur}：<b>', '${esc(cur)}：<b>'),
        ('${x.seriesName}：<b>', '${esc(x.seriesName)}：<b>'),
        ('${c}</td>', '${esc(c)}</td>'),
        ('${g}</span>', '${esc(g)}</span>'),
        ('onclick="pick(\'${c}\')"', 'onclick="pick(PAY.com[${COM.indexOf(c)}])"'),
        ('</span>${c}`', '</span>${esc(c)}`'),
    ]
    for old, new in replacements:
        if page.count(old) != 1:
            raise ValueError(f"参考 HTML 结构已变化，无法替换：{old}")
        page = page.replace(old, new, 1)
    marker = '    $("#rng").textContent'
    helpers = """    function esc(value) {
        return String(value).replace(/[&<>"']/g, c => ({
            "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
        }[c]));
    }
    COM.forEach((name, i) => {
        if (!Object.hasOwn(COLORS, name)) COLORS[name] = ["#5fa8d3", "#41b883", "#9d8ec7", "#e6704b"][i % 4];
    });

"""
    return page.replace(marker, helpers + marker, 1)


def generate_html(source=INPUT, output=None, generated_at=None, template_path=TEMPLATE):
    generated_at = generated_at or datetime.now()
    output = Path(output) if output is not None else ROOT / "html" / f"{generated_at:%Y%m%d}_material_prices.html"
    if output.resolve() in (Path(template_path).resolve(), Path(source).resolve()):
        raise ValueError("输出文件不能覆盖参考 HTML 或输入 Excel")
    template = Path(template_path).read_text(encoding="utf-8")
    start = template.index("    const PAY = ") + len("    const PAY = ")
    end = template.index("    const D = PAY.series", start)
    original = json.loads(template[start:end].strip().removesuffix(";"))
    names, daily = read_prices(source, generated_at.date())
    payload = build_payload(names, daily, generated_at, original["group"])
    page = render_html(template, payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT, help="输入 Excel 文件")
    parser.add_argument("--output", type=Path, help="输出 HTML 文件")
    args = parser.parse_args()
    output = generate_html(args.input, args.output)
    print(f"已生成：{output}")


if __name__ == "__main__":
    main()
