# -*- coding: utf-8 -*-
"""全量校验：把插件里的零依赖农历实现和 lunar_python（天文算法权威源）
逐日对比，覆盖 1900-01-31 ~ 2100-12-31（73412 天）。

比对项：农历年 / 月 / 日、闰月标志、传统节日、公历节日、寒食节、24 节气。

性能提示（Pi Zero 单核）：
- 生成期望值用 `Lunar.fromYmd(Y, m, 1).getSolar()` 取农历月首日，不要逐日 Solar->Lunar。
- 节日期望值直接查 `LunarUtil.FESTIVAL` 表，不要逐日构造 Lunar 对象
  （每次构造都会重建该农历年的 25 个节气时刻，非常慢）。
"""
import datetime
import sys

sys.path.insert(0, "/home/pi/InkyPi-main/src/plugins/duo_calendar")

from lunar_python import Solar, Lunar  # noqa: E402
from lunar_python.util import LunarUtil, SolarUtil  # noqa: E402
import lunar as my  # noqa: E402

MIN_YEAR = 1900
MAX_YEAR = 2100
TERMS = ["小寒", "大寒", "立春", "雨水", "惊蛰", "春分",
         "清明", "谷雨", "立夏", "小满", "芒种", "夏至",
         "小暑", "大暑", "立秋", "处暑", "白露", "秋分",
         "寒露", "霜降", "立冬", "小雪", "大雪", "冬至"]


def solar_date(lunar_obj):
    y, m, d = [int(x) for x in lunar_obj.toYmd().split("-")]
    return datetime.date(y, m, d)


def reference_solar_festival(day):
    """参照 lunar_python 的 Solar.getFestivals() 算法算公历节日。

    注意：星期类节日（母亲节/感恩节…）与年份的星期分布有关，
    不能用某一年的结果套到所有年份 —— 必须按 lunar_python 的规则逐日算。
    """
    name = SolarUtil.FESTIVAL.get("%d-%d" % (day.month, day.day))
    if name:
        return name
    week = (day.weekday() + 1) % 7                 # 0=周日
    index = -(-day.day // 7)                        # ceil(day/7)
    name = SolarUtil.WEEK_FESTIVAL.get("%d-%d-%d" % (day.month, index, week))
    if name:
        return name
    if day.day + 7 > SolarUtil.getDaysOfMonth(day.year, day.month):
        name = SolarUtil.WEEK_FESTIVAL.get("%d-0-%d" % (day.month, week))
        if name:
            return name
    return None


def main():
    # ---- 节气总表：把农历年的节气表摊平成 (公历年, 月, 日) -> 名称
    term_by_ymd = {}
    for ly in range(MIN_YEAR, MAX_YEAR + 2):
        table = Solar.fromYmd(ly, 1, 1).getLunar().getJieQiTable()
        for name in TERMS:
            s = table[name]
            term_by_ymd[(s.getYear(), s.getMonth(), s.getDay())] = name

    # ---- 表级一致性：插件里的节日表必须和 lunar_python 的权威表逐键相等
    assert my._GREGORIAN_FESTIVAL == SolarUtil.FESTIVAL, "公历节日表与 lunar_python 不一致"
    assert my._WEEK_FESTIVAL == SolarUtil.WEEK_FESTIVAL, "星期节日表与 lunar_python 不一致"
    assert my.LUNAR_FESTIVAL == LunarUtil.FESTIVAL, "农历节日表与 lunar_python 不一致"
    print("festival tables     : 与 lunar_python 完全一致")

    checked = 0
    bad_date = 0
    bad_festival = 0
    bad_term = 0
    date_samples = []
    fest_samples = []
    term_samples = []

    for y in range(MIN_YEAR, MAX_YEAR + 1):
        # 该农历年的月份序列（含闰月）
        leap = 0
        for m in range(1, 13):
            try:
                if Lunar.fromYmd(y, -m, 1).getMonth() == -m:
                    leap = m
                    break
            except Exception:
                continue
        signed = []
        for m in range(1, 13):
            signed.append(m)
            if m == leap:
                signed.append(-m)

        # 每个月的确切天数
        lengths = []
        for index, s in enumerate(signed):
            start = solar_date(Lunar.fromYmd(y, s, 1).getSolar())
            if index + 1 < len(signed):
                end = solar_date(Lunar.fromYmd(y, signed[index + 1], 1).getSolar())
            else:
                try:
                    end = solar_date(Lunar.fromYmd(y + 1, 1, 1).getSolar())
                except Exception:
                    end = None
                    probe = start
                    for _ in range(60):
                        probe = probe + one
                        if Solar.fromYmd(probe.year, probe.month, probe.day).getLunar().getYear() != y:
                            end = probe
                            break
                    if end is None:
                        raise RuntimeError("no next year for %d" % y)
            lengths.append((end - start).days)
        last_month_len = lengths[-1]

        for index, s in enumerate(signed):
            start = solar_date(Lunar.fromYmd(y, s, 1).getSolar())
            length = lengths[index]

            for k in range(1, length + 1):
                day = start + datetime.timedelta(days=k - 1)
                mine = my.solar_to_lunar(day)
                checked += 1

                # ---- 农历年月日 + 闰月标志
                if mine is None:
                    bad_date += 1
                    if len(date_samples) < 40:
                        date_samples.append("DATE %s expect=%s/%s got=None"
                                            % (day, (y, abs(s), k), s < 0))
                else:
                    expect = (y, abs(s), k)
                    got = (mine.year, mine.month, mine.day)
                    if expect != got or mine.leap != (s < 0):
                        bad_date += 1
                        if len(date_samples) < 40:
                            date_samples.append("DATE %s expect=%s/%s got=%s/%s"
                                                % (day, expect, s < 0, got, mine.leap))

                # ---- 节日：农历传统节日 > 除夕 > 公历节日 > 寒食节
                expect_f = LunarUtil.FESTIVAL.get("%d-%d" % (s, k))
                if expect_f is None and s == signed[-1] and k == last_month_len:
                    expect_f = "除夕"
                if expect_f is None:
                    expect_f = reference_solar_festival(day)
                if expect_f is None and day.month == 4:
                    qingming = None
                    for dd in range(1, 32):
                        if term_by_ymd.get((day.year, 4, dd)) == "清明":
                            qingming = dd
                            break
                    if qingming is not None and day.day == qingming - 1:
                        expect_f = "寒食节"
                got_f = my.festival(day, mine) if mine is not None else None
                if expect_f != got_f:
                    bad_festival += 1
                    if len(fest_samples) < 20:
                        fest_samples.append("FEST %s expect=%s got=%s" % (day, expect_f, got_f))

                # ---- 节气（插件的节气表只覆盖 1900-2100）
                expect_t = term_by_ymd.get((day.year, day.month, day.day))
                if day.year > MAX_YEAR:
                    expect_t = None
                got_t = my.solar_term(day)
                if expect_t != got_t:
                    bad_term += 1
                    if len(term_samples) < 40:
                        term_samples.append("TERM %s expect=%s got=%s" % (day, expect_t, got_t))

        if y % 20 == 0:
            print("  ... year %d done (%d days)" % (y, checked), file=sys.stderr)

    print("checked days        : %d" % checked)
    print("date mismatches     : %d" % bad_date)
    print("festival mismatches : %d" % bad_festival)
    print("term mismatches     : %d" % bad_term)
    for s in date_samples:
        print("  " + s)
    for s in term_samples:
        print("  " + s)
    for s in fest_samples:
        print("  " + s)

    ok = (bad_date == 0 and bad_festival == 0 and bad_term == 0)
    print("RESULT: %s" % ("ALL MATCH" if ok else "MISMATCH FOUND"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
