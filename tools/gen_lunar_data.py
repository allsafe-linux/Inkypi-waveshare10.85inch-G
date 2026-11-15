# -*- coding: utf-8 -*-
"""开发期工具：从 lunar_python（天文算法权威源）抽取紧凑数据表，
生成插件用的零依赖 lunar_data.py。

用 Lunar.fromYmd(年, 月, 1).getSolar() 直接取农历月首日，
比逐日换算快 100 多倍（Pi Zero 上 17 分钟 -> 1 分钟）。

生成物：
  LUNAR_YEAR_DATA  {农历年: "闰月码" + 12个月大小 + "闰月大小"}
  SOLAR_TERM_DATA  {公历年: 24个节气的"日"（base32 编码）}
  LUNAR_FESTIVAL   {"农历月-日": 名称}
  ANCHOR           公历 -> 农历 的锚点
"""
import datetime
import os
import sys

from lunar_python import Solar, Lunar
from lunar_python.util import LunarUtil

TERMS = ["小寒", "大寒", "立春", "雨水", "惊蛰", "春分",
         "清明", "谷雨", "立夏", "小满", "芒种", "夏至",
         "小暑", "大暑", "立秋", "处暑", "白露", "秋分",
         "寒露", "霜降", "立冬", "小雪", "大雪", "冬至"]
TERM_MONTHS = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
               7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12]

B32 = "0123456789ABCDEFGHIJKLMNOPQRSTUV"


def b32(n):
    assert 0 <= n <= 31, n
    return B32[n]


def solar_date(lunar_obj):
    y, m, d = [int(x) for x in lunar_obj.toYmd().split("-")]
    return datetime.date(y, m, d)


def month_start(lunar_year, signed_month):
    return solar_date(Lunar.fromYmd(lunar_year, signed_month, 1).getSolar())


def find_leap_month(lunar_year):
    for m in range(1, 13):
        try:
            if Lunar.fromYmd(lunar_year, -m, 1).getMonth() == -m:
                return m
        except Exception:
            continue
    return 0


def month_lengths(lunar_year):
    """返回 (闰月月份, [12 个月大小], 闰月大小)"""
    leap = find_leap_month(lunar_year)
    signed = []
    for m in range(1, 13):
        signed.append(m)
        if m == leap:
            signed.append(-m)

    lengths = []
    for index, s in enumerate(signed):
        start = month_start(lunar_year, s)
        if index + 1 < len(signed):
            end = month_start(lunar_year, signed[index + 1])
        else:
            try:
                end = month_start(lunar_year + 1, 1)
            except Exception:
                # 2100 年末：逐日兜底
                end = _scan_next_year_start(lunar_year, start)
        lengths.append((end - start).days)

    month_days = []
    leap_days = 0
    for index, s in enumerate(signed):
        if s < 0:
            leap_days = lengths[index]
        else:
            month_days.append(lengths[index])
    return leap, month_days, leap_days


def _scan_next_year_start(lunar_year, after_date):
    """年末兜底：往后逐日找农历年变成 lunar_year+1 的那天。"""
    d = after_date
    one = datetime.timedelta(days=1)
    for _ in range(60):
        d = d + one
        l = Solar.fromYmd(d.year, d.month, d.day).getLunar()
        if l.getYear() != lunar_year:
            return d
    raise RuntimeError("cannot find next lunar year start for %d" % lunar_year)


def main():
    # 锚点校验：1900-01-31 必须是农历 1900 正月初一
    anchor = solar_date(Lunar.fromYmd(1900, 1, 1).getSolar())
    assert anchor == datetime.date(1900, 1, 31), "anchor mismatch: %s" % anchor
    back = Solar.fromYmd(1900, 1, 31).getLunar()
    assert (back.getYear(), back.getMonth(), back.getDay()) == (1900, 1, 1), "anchor reverse mismatch"

    # ---------------------------------------------------------- 1) 农历月结构
    year_data = {}
    for y in range(1900, 2101):
        leap, month_days, leap_days = month_lengths(y)
        if len(month_days) != 12:
            raise SystemExit("bad year %d" % y)
        for d in month_days:
            if d not in (29, 30):
                raise SystemExit("bad month length %d in %d" % (d, y))
        code = b32(leap)
        code += "".join(b32(dd - 29) for dd in month_days)
        code += b32(leap_days - 29) if leap else "0"
        year_data[y] = code
        if y % 20 == 0:
            print("  lunar years ... %d" % y, file=sys.stderr)

    # ---------------------------------------------------------- 2) 节气
    # 注意：农历年的节气表跨到相邻公历年（1900 农历年的冬至落在 1899-12-22），
    # 所以先把所有节气摊平成 (公历年, 月, 日) -> 名称，再按公历年重建。
    term_by_ymd = {}
    for ly in range(1900, 2102):
        table = Solar.fromYmd(ly, 1, 1).getLunar().getJieQiTable()
        for name in TERMS:
            s = table[name]
            term_by_ymd[(s.getYear(), s.getMonth(), s.getDay())] = name
        if ly % 20 == 0:
            print("  solar years ... %d" % ly, file=sys.stderr)

    term_data = {}
    for y in range(1900, 2102):
        row = []
        for index, name in enumerate(TERMS):
            m = TERM_MONTHS[index]
            found = None
            for d in range(1, 32):
                if term_by_ymd.get((y, m, d)) == name:
                    found = d
                    break
            if found is None:
                raise SystemExit("solar term %s not found in %d-%d" % (name, y, m))
            row.append(b32(found - 1))
        term_data[y] = "".join(row)

    # ---------------------------------------------------------- 3) 节日
    lunar_festival = dict(LunarUtil.FESTIVAL)

    # ---------------------------------------------------------- 4) 输出
    out = []
    out.append("# -*- coding: utf-8 -*-")
    out.append('"""农历数据表（自动生成，请勿手工编辑）。')
    out.append("")
    out.append("数据由 lunar_python (MIT, https://github.com/6tail/lunar-python) 的")
    out.append("寿星天文算法在 1900-2100 全量抽取，作为权威参照生成本表。")
    out.append("生成脚本：tools/gen_lunar_data.py")
    out.append('"""')
    out.append("")
    out.append("# 每年一条：首字符 = 闰月月份（'0'..'9','A','B','C'，0 表示无闰月）")
    out.append("# 接着 12 个字符 = 12 个月的大小（'0'=29天, '1'=30天）")
    out.append("# 最后 1 个字符 = 闰月大小（'0'=29天, '1'=30天）")
    out.append("LUNAR_YEAR_DATA = {")
    for y in sorted(year_data):
        out.append("    %d: '%s'," % (y, year_data[y]))
    out.append("}")
    out.append("")
    out.append("# 每年 24 个节气的日（公历），base32 编码：char 的序号 + 1 = 日")
    out.append("# 顺序 = 小寒 大寒 立春 雨水 惊蛰 春分 清明 谷雨 立夏 小满 芒种 夏至")
    out.append("#        小暑 大暑 立秋 处暑 白露 秋分 寒露 霜降 立冬 小雪 大雪 冬至")
    out.append("SOLAR_TERM_DATA = {")
    for y in sorted(term_data):
        out.append("    %d: '%s'," % (y, term_data[y]))
    out.append("}")
    out.append("")
    out.append("# 传统节日（农历 月-日）")
    out.append("LUNAR_FESTIVAL = {")
    for k in sorted(lunar_festival, key=lambda x: tuple(int(i) for i in x.split("-"))):
        out.append("    '%s': '%s'," % (k, lunar_festival[k]))
    out.append("}")
    out.append("")
    out.append("# 公历 -> 农历 锚点：1900-01-31 是农历 1900 年正月初一")
    out.append("ANCHOR_SOLAR = (1900, 1, 31)")
    out.append("ANCHOR_LUNAR = (1900, 1, 1)")
    out.append("")
    out.append("TERM_NAMES = %r" % (TERMS,))
    out.append("TERM_MONTHS = %r" % (TERM_MONTHS,))
    out.append("")

    dest = sys.argv[1]
    with open(dest, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print("written:", dest, os.path.getsize(dest), "bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
