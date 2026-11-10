# 生意社（100ppi.com）价格数据抓取方法

> 记录于 2026-11-03。用于复现「各材料近一年逐日价格」的抓取。
> 相关产出：`sql/01_material_<code>_data.sql`

---

## 一、结论速览

| 想要的 | 能否免费拿到 | 接口 |
|---|---|---|
| **近一年逐日现货价**（366 天） | ✅ | `graph/cindex.php?f=graph_ave&ppid=<id>&type=1&mode=2` ★核心 |
| 近 3 个月逐日现货价 | ✅ | `graph/cindex.php?f=graph_ppid_ave&ppid=<id>` |
| 当前基准价 / 涨跌 / 三月均高最低 / 年位置 | ✅ | `rawmex/detail-<id>.html` |
| 生意社商品指数（近一年） | ✅ | 同核心接口，`type=2` |
| 商家报价（含厂商、交货地） | ✅ | `mprice/` 列表页 + `mprice/Index/pmore` |
| 期货结算价（按日） | ✅ | `futures.100ppi.com/qhb/day-YYYY-MM-DD.html` |
| 基差率年度对比（按年分线） | ✅ | `graph/future.php?f=futures_date&ppid=<id>` |
| 近 10 年全量历史 | ❌ 付费 | 产业通账号 |

**关键点**：站上图表默认只展示 3 个月，且该接口**不响应任何区间参数**（见第六节踩坑记录）。近一年数据要走 `f=graph_ave` 这个另一套接口。

---

## 二、反爬机制

直接访问会命中一个 660 字节的「安全检查」页：

```html
<script>
  var _0x2 = "cf1451e2fb09378b696088d40a857a47";
  document.cookie = "HW_CHECK" + "=" + _0x2 + "; expires=...; path=/";
  setTimeout(function(){ location.href = location.href; }, 300 + ...);
</script>
<p>正在进行安全检查，请稍候...</p>
```

处理方式（脚本已自动完成）：

1. 首次请求若响应体含「安全检查」，用正则取出 `_0x2` 的值；
2. 以 `Cookie: HW_CHECK=<值>` 重试；
3. 该 cookie 值在较长时间内**固定复用即可**，实测连续数百次请求有效。发现失效时脚本会自动重新取一次。

> 注意：该 cookie 与 IP 无关，但**与请求频率有关**，批量抓取务必限速（建议 0.5–1s/次）。

---

## 三、核心接口：近一年逐日价格

```
https://www.100ppi.com/graph/cindex.php?f=graph_ave&ppid=<商品ID>&type=1&mode=2
```

- 返回一个用 echarts 渲染的 HTML 片段，**数据内嵌在 JS 里并被 packer 混淆**（见第四节）。
- `Referer` 建议带 `https://hrb.100ppi.com/`（品种专站），不加通常也能通。

### type / mode 参数矩阵（以 ppid=927 螺纹钢 实测）

| type | mode | 结果 |
|---|---|---|
| **1** | **2** | ✅ **现货价格，366 点，2025-11-03 ~ 2026-11-03** |
| 1 | 3 | ✅ 同上 |
| 2 | 2 / 3 | ✅ 生意社商品指数，366 点 |
| 0 / 3 / 4 | 0–5 | ❌ 无日期数据 |
| 1 / 2 | 0 / 1 / 4 / 5 | ❌ 无日期数据 |

- `type=1` → 现货价格，图表 `text` 字段形如「螺纹钢生意社价格」，`subtext` 是计量口径（如「杭州市 市场价 HRB400;φ16-20」）。
- `type=2` → 指数。

### 解出来的数据结构

- `xAxis.data` → 日期数组 `['2025-11-03', ...]`，**自然日连续，含周末节假日**
- `series[].data` → 价格数组，与日期一一对应
- 非交易日沿用上一日价格（周末数值常与周五相同）

---

## 四、packer 解包

页面里的 `eval(function(p,a,c,k,e,d){...})` 是标准 **Dean Edwards packer**。
两处易错，务必注意：

1. 字母表是 `0-9a-zA-Z`（62 进制），不是纯 `0-9a-z`；
2. 查词表用的是 `words[idx]`（**0-based**），不是 `words[idx-1]`。

```python
ALPHA = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

def _unbase(s, base):
    n = 0
    for ch in s:
        n = n * base + ALPHA.index(ch)
    return n

def unpack(src):
    m = re.search(
        r"eval\(function\(p,a,c,k,e,d\).*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)",
        src, re.S)
    if not m:
        return None
    payload, base, words = m.group(1), int(m.group(2)), m.group(4).split("|")
    payload = payload.replace("\\'", "'").replace("\\\\", "\\")
    words = [w.replace("\\'", "'") for w in words]

    def repl(mm):
        idx = _unbase(mm.group(0), base)
        if 0 <= idx < len(words) and words[idx]:
            return words[idx]
        return mm.group(0)

    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)
```

> 若 `unpack()` 返回 `None`，说明该页面**没有被混淆**（如 `f=futures_date`），直接用原文正则即可。

---

## 五、完整可运行脚本

已实测通过（含反爬自动重试）。存为 `fetch_100ppi.py` 即可用。

```python
# -*- coding: utf-8 -*-
"""生意社(100ppi)逐日价格抓取。"""
import re, ssl, time, urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
ALPHA = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_cookie = {"HW_CHECK": "cf1451e2fb09378b696088d40a857a47"}


def _raw_get(url, referer, cookie):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookie.items()),
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=25, context=_CTX) as r:
        return r.read()


def _parse_check_cookie(html):
    """从'安全检查'页里取出 HW_CHECK 值。"""
    m = re.search(r'var\s+_0x2\s*=\s*"([0-9a-fA-F]+)"', html)
    return m.group(1) if m else None


def fetch(url, referer="https://www.100ppi.com/", timeout=25):
    """GET 并自动处理 HW_CHECK 反爬校验。"""
    for _ in range(3):
        raw = _raw_get(url, referer, _cookie)
        head = raw[:400].decode("utf-8", "ignore")
        if "安全检查" in head:
            val = _parse_check_cookie(head)
            if not val:
                raise RuntimeError("无法解析 HW_CHECK cookie")
            _cookie["HW_CHECK"] = val
            time.sleep(0.8)
            continue
        return raw
    raise RuntimeError("反爬校验未通过")


def _unbase(s, base):
    n = 0
    for ch in s:
        n = n * base + ALPHA.index(ch)
    return n


def unpack(src):
    """还原 Dean Edwards packer；未混淆时返回 None。"""
    m = re.search(
        r"eval\(function\(p,a,c,k,e,d\).*?\}\('(.*?)',(\d+),(\d+),'(.*?)'\.split\('\|'\)",
        src, re.S)
    if not m:
        return None
    payload, base, words = m.group(1), int(m.group(2)), m.group(4).split("|")
    payload = payload.replace("\\'", "'").replace("\\\\", "\\")
    words = [w.replace("\\'", "'") for w in words]

    def repl(mm):
        idx = _unbase(mm.group(0), base)
        if 0 <= idx < len(words) and words[idx]:
            return words[idx]
        return mm.group(0)

    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)


def parse_chart(raw):
    """抽出 (dates, values, title, subtext)。"""
    s = raw.decode("utf-8", "ignore")
    out = unpack(s) or s
    xm = re.search(r"xAxis:\{[^}]*data:\[(.*?)\]", out, re.S)
    dates = re.findall(r"'(20\d\d-\d\d-\d\d)'", xm.group(1)) if xm else []
    sm = re.search(r"series:\s*\[.*?data:\[(.*?)\]\s*\}", out, re.S)
    vals = re.findall(r"-?\d+\.?\d*", sm.group(1)) if sm else []
    ttl = re.findall(r"(?<!sub)text:\s*'([^']+)'", out)
    sub = re.findall(r"subtext:\s*'([^']+)'", out)
    n = min(len(dates), len(vals))
    return dates[-n:], vals[-n:], ttl, sub


YEAR_URL = "https://www.100ppi.com/graph/cindex.php?f=graph_ave&ppid={pid}&type=1&mode=2"


def get_year(ppid):
    """取某商品近一年逐日现货价 -> (dates, values, title, subtext)"""
    return parse_chart(fetch(YEAR_URL.format(pid=ppid), referer="https://hrb.100ppi.com/"))


if __name__ == "__main__":
    for code, pid in [("rebar", 927), ("copper", 524), ("tin", 492)]:
        d, v, ttl, sub = get_year(pid)
        print(f"{code:8s} {len(d)}点 {d[0]}..{d[-1]} 尾={v[-1]}  {ttl[:1]}")
        time.sleep(0.6)
```

---

## 六、商品 ID（ppid）怎么找

`ppid` 与「单品基准价页」`rawmex/detail-<ppid>.html` 的编号**是同一个**。

从基准价格目录页 `https://www.100ppi.com/monitor/` 一次性抓取全部映射（实测约 450 个品种）：

```python
h = fetch("https://www.100ppi.com/monitor/").decode("utf-8", "ignore")
pairs = re.findall(r'<a[^>]+href="/rawmex/detail-(\d+)\.html"[^>]*>(.*?)</a>', h, re.S)
# -> [('927', '螺纹钢'), ('524', '铜'), ...]
```

### 本项目已用到的 11 个

| code | 材料 | ppid | 口径 |
|---|---|---|---|
| `copper` | 铜 | 524 | 上海 1#电解铜 |
| `aluminum` | 铝 | 482 | 华东 重熔用铝锭 Al99.70 |
| `lead` | 铅 | 379 | 华东 1#铅锭 Pb99.994 |
| `zinc` | 锌 | 431 | 华东 0#锌锭 Zn99.995 |
| `nickel` | 镍 | 432 | 上海 1#镍 Ni9990 |
| `tin` | 锡 | 492 | 华东 1#锡锭 Sn99.90 |
| `rebar` | 螺纹钢 | 927 | 杭州 HRB400 φ16-20 |
| `hot_rolled_coil` | 热轧卷板 | 195 | 杭州 7.5*1500*C |
| `wire_rod` | 线材 | 740 | 江浙沪 HPB300 Φ8 |
| `iron_ore` | 铁矿石 | 961 | 澳大利亚 62% 湿吨 |
| `stainless_steel` | 不锈钢 | 1300 | 304/2B 冷轧卷板 2.0*1219*C |

> ⚠️ 生意的「不锈钢」有多个 ppid：1300（304/2B 冷轧卷板）、634（不锈钢板）、1570（不锈钢卷）。本项目取 1300。

---

## 七、踩坑记录（别重复走）

| 尝试 | 结果 |
|---|---|
| `f=graph_ppid_ave&ppid=927` | 只有 93 天（3 个月） |
| 给它加 `sdate/edate/st/et/start/end/begin/from/to/days/range/period/n/limit/...` 共 40+ 个参数名 | **全部无效**，永远 93 天 |
| `f=graph_ppid_year / _1y / _max / _all / _hist / _date` 等 | 返回空 |
| `graph.100ppi.com/future.php?f=graph&btime=&etime=` | 支持任意区间，但返回的是 **PNG 图片**，不是数据 |
| `f=futures_date` | 有年度对比数据，但内容是**基差率**（按年份分线），不是现货价 |
| 桌面版 `/vane/927-螺纹钢.html` | 404 |
| `m.100ppi.com` | 404（移动版是 `m1.100ppi.com`） |
| `m1.100ppi.com/vane/<ppid>-<名称>` | 有逐日价格表，但**静态只给 7 条** |
| `mprice/Index/pmore`（POST `p`,`title`） | 是**商家报价**分页，不是基准价序列 |
| 数据中心 `/data/` | 是产品介绍页，历史下载需付费 |
| **`f=graph_ave&type=1&mode=2`** | ✅ **答案在这里** |

补充：`f=graph_ave` 这个接口名是从**品种专站**页面（如 `hrb.100ppi.com`）里发现的 —— 主站单品页只挂了 `graph_ppid_ave`。以后找不到数据时，去对应品种专站看看。

---

## 八、写成 SQL 的约定

生成 `sql/01_material_<code>_data.sql` 时遵循：

```sql
set @material_id = (select id from material where code = 'copper');

insert into material_price (material_id, source_id, price, stat_date)
values
    (@material_id, 1, 86941.67, '2025-11-03 00:00:00'),
    ...
    (@material_id, 1, 109951.67, '2026-11-03 00:00:00');
```

| 约定 | 说明 |
|---|---|
| `material_id` | 用 `code` 子查询，**不写死自增 ID** |
| `source_id` | 硬编码 `1` = 生意社。⚠️ `material_source` 表目前**无种子数据**，若库中 id 不同需替换 |
| `price` | 直接取抓到的两位小数字符串 |
| `stat_date` | `YYYY-MM-DD 00:00:00` |
| 日期 | **自然日连续**（含周末，非交易日沿用前值），共 366 天 |
| 重复执行 | 表上**无** `(material_id, source_id, stat_date)` 唯一索引，会插重复。文件中留有注释掉的 `delete` 供启用 |

`price` 是 `decimal(10,2)`，上限 99,999,999.99；锡约 36 万、铜约 11 万，均安全。

### 本机环境备忘

- 项目 venv **没有 pip**（用 `uv` 管理）；
- 校验 SQL 语法可用：`uv run --no-project --with sqlglot python <脚本>`
- 本机**未安装 MySQL**，SQL 只能做离线解析校验，不能实跑。

---

## 九、注意事项

1. **限速**：建议每次请求间隔 0.5–1 秒，批量抓 11 个品种约十几秒。
2. **只读抓取**，不做登录、不碰付费区。
3. 数值口径以图表 `subtext` 为准（产地/规格/交货地不同会显著影响价格），写库前建议一并记录。
4. 页面结构若改版，优先检查：`_0x2` 变量名、packer 正则、`xAxis/series` 字段名。
