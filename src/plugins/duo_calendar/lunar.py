# -*- coding: utf-8 -*-
"""零依赖农历换算。

数据来自 lunar_data.py（由 lunar_python 的寿星天文算法在 1900-2100 全量抽取），
本模块只做查表和格式换算，不依赖任何第三方包。

对外接口：
    solar_to_lunar(date)          -> LunarDate(年, 月, 日, 是否闰月)
    lunar_day_text(day)           -> "初一" ... "三十"
    year_ganzhi(lunar_year)       -> "丙午"
    year_zodiac(lunar_year)       -> "马"
    day_label(date, lunar)        -> 该格要显示的小字（节日 > 节气 > 农历日）
    lunar_header_text(date, lunar)-> "丙午年 十月初六"
"""

from datetime import date, timedelta
import bisect

try:
    from plugins.duo_calendar.lunar_data import (
        LUNAR_YEAR_DATA,
        SOLAR_TERM_DATA,
        LUNAR_FESTIVAL,
        TERM_NAMES,
        TERM_MONTHS,
    )
except ImportError:  # 直接以脚本方式运行时（开发期校验）走相对导入
    from lunar_data import (  # type: ignore
        LUNAR_YEAR_DATA,
        SOLAR_TERM_DATA,
        LUNAR_FESTIVAL,
        TERM_NAMES,
        TERM_MONTHS,
    )

MIN_YEAR = 1900
MAX_YEAR = 2100

_ANCHOR_SOLAR = date(1900, 1, 31)   # 农历 1900 年正月初一
_BASE32 = "0123456789ABCDEFGHIJKLMNOPQRSTUV"

_GAN = "甲乙丙丁戊己庚辛壬癸"
_ZHI = "子丑寅卯辰巳午未申酉戌亥"
_ZODIAC = "鼠牛虎兔龙蛇马羊猴鸡狗猪"

_DAY_TEXT = (
    "初一", "初二", "初三", "初四", "初五", "初六",
    "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六",
    "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六",
    "廿七", "廿八", "廿九", "三十",
)

# 农历月份一律用中文（不要阿拉伯数字）：
# 「10月初九」写成「十月初九」。
# 传统上农历正月叫「正月」，冬月/腊月对应十一月/十二月，这里统一用「十一月/十二月」更明确。
_CN_MONTHS = (
    "正月", "二月", "三月", "四月", "五月", "六月",
    "七月", "八月", "九月", "十月", "十一月", "十二月",
)

# 公历节日（公历 月-日），与 lunar_python 的 SolarUtil.FESTIVAL 对齐
_GREGORIAN_FESTIVAL = {
    "1-1": "元旦节",
    "2-14": "情人节",
    "3-8": "妇女节",
    "3-12": "植树节",
    "3-15": "消费者权益日",
    "4-1": "愚人节",
    "5-1": "劳动节",
    "5-4": "青年节",
    "6-1": "儿童节",
    "7-1": "建党节",
    "8-1": "建军节",
    "9-10": "教师节",
    "10-1": "国庆节",
    "10-31": "万圣节前夜",
    "11-1": "万圣节",
    "12-24": "平安夜",
    "12-25": "圣诞节",
}

# 按星期的节日：月份-第几周-星期几（0=周日）
# 第几周 = ceil(日/7)；0 表示「该月最后一周」
_WEEK_FESTIVAL = {
    "3-0-1": "全国中小学生安全教育日",   # 3 月最后一个周一
    "5-2-0": "母亲节",                   # 5 月第二个周日
    "5-3-0": "全国助残日",               # 5 月第三个周日
    "6-3-0": "父亲节",                   # 6 月第三个周日
    "9-3-6": "全民国防教育日",           # 9 月第三个周六
    "10-1-1": "世界住房日",              # 10 月第一个周一
    "11-4-4": "感恩节",                  # 11 月第四个周四
}


class LunarDate(object):
    """农历日期。month 为正数，leap 为 True 表示闰月。"""

    __slots__ = ("year", "month", "day", "leap")

    def __init__(self, year, month, day, leap=False):
        self.year = year
        self.month = month
        self.day = day
        self.leap = leap

    @property
    def signed_month(self):
        return -self.month if self.leap else self.month

    @property
    def month_text(self):
        """农历月份的中文写法，如「正月」「十月」「闰四月」。"""
        if 1 <= self.month <= 12:
            name = _CN_MONTHS[self.month - 1]
        else:
            name = "%d月" % self.month
        return ("闰" if self.leap else "") + name

    @property
    def day_text(self):
        return _DAY_TEXT[self.day - 1]

    def __repr__(self):
        return "LunarDate(%d, %d, %d, leap=%s)" % (self.year, self.month, self.day, self.leap)


# ------------------------------------------------------------------ 查表
def _decode(code):
    """把 14 个字符的年份编码拆成 (闰月, 12 个月大小, 闰月大小)"""
    leap_month = _BASE32.index(code[0])
    month_days = [29 + _BASE32.index(code[1 + i]) for i in range(12)]
    leap_days = 29 + _BASE32.index(code[13])
    return leap_month, month_days, leap_days


def leap_month(lunar_year):
    if lunar_year < MIN_YEAR or lunar_year > MAX_YEAR:
        return 0
    return _decode(LUNAR_YEAR_DATA[lunar_year])[0]


def month_days(lunar_year, month, leap=False):
    _, month_days_list, leap_days = _decode(LUNAR_YEAR_DATA[lunar_year])
    if leap:
        return leap_days
    return month_days_list[month - 1]


def lunar_year_days(lunar_year):
    _, month_days_list, leap_days = _decode(LUNAR_YEAR_DATA[lunar_year])
    total = sum(month_days_list)
    if leap_month(lunar_year):
        total += leap_days
    return total


# 每个农历年首日相对锚点的累计天数，二分查找用（懒加载）
_YEAR_OFFSETS = None


def _year_offsets():
    global _YEAR_OFFSETS
    if _YEAR_OFFSETS is None:
        offsets = []
        total = 0
        for y in range(MIN_YEAR, MAX_YEAR + 1):
            offsets.append(total)
            total += lunar_year_days(y)
        _YEAR_OFFSETS = offsets
    return _YEAR_OFFSETS


def solar_to_lunar(day):
    """公历 -> 农历。超出 1900-2100 返回 None。

    注意：农历 2100 年的最后一个月会延伸到公历 2101 年 1~2 月，
    所以上界不能简单用 `day.year > 2100` 判断，要用累计天数。
    """
    if day < _ANCHOR_SOLAR:
        return None

    remaining = (day - _ANCHOR_SOLAR).days
    offsets = _year_offsets()
    index = bisect.bisect_right(offsets, remaining) - 1
    if index < 0 or index >= len(offsets):
        return None
    year = MIN_YEAR + index
    remaining -= offsets[index]

    lm = leap_month(year)
    month = 1
    while True:
        size = month_days(year, month)
        if remaining < size:
            return LunarDate(year, month, remaining + 1, False)
        remaining -= size
        if lm == month:
            size = month_days(year, month, leap=True)
            if remaining < size:
                return LunarDate(year, month, remaining + 1, True)
            remaining -= size
        month += 1
        if month > 12:
            return None


def lunar_day_text(day):
    if 1 <= day <= 30:
        return _DAY_TEXT[day - 1]
    return ""


def year_ganzhi(lunar_year):
    index = (lunar_year - 4) % 60
    return _GAN[index % 10] + _ZHI[index % 12]


def year_zodiac(lunar_year):
    return _ZODIAC[(lunar_year - 4) % 12]


# ------------------------------------------------------------------ 节日 / 节气
def solar_term(day):
    """公历日期对应的节气名，不是节气则返回 None。"""
    if day.year < MIN_YEAR or day.year > MAX_YEAR:
        return None
    code = SOLAR_TERM_DATA.get(day.year)
    if not code:
        return None
    for index, term_month in enumerate(TERM_MONTHS):
        if term_month == day.month and _BASE32.index(code[index]) + 1 == day.day:
            return TERM_NAMES[index]
    return None


def _is_last_day_of_lunar_year(lunar):
    """该农历日是不是这一农历年的最后一天（除夕判定用）"""
    if lunar.leap:
        # 闰月只有排在 12 月时，腊月最后一天才是年末
        if lunar.month != 12:
            return False
        return lunar.day == month_days(lunar.year, 12, leap=True)
    if lunar.month != 12:
        return False
    return lunar.day == month_days(lunar.year, 12) and leap_month(lunar.year) != 12


def _days_in_month(year, month):
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def gregorian_week_festival(day):
    """按星期几定的节日（母亲节/父亲节/感恩节…）。"""
    month = day.month
    weekday = (day.weekday() + 1) % 7          # 0=周日
    week_index = (day.day - 1) // 7 + 1        # 该月第几周
    name = _WEEK_FESTIVAL.get("%d-%d-%d" % (month, week_index, weekday))
    if name:
        return name
    # 该月最后一周的特例（lunar_python 用 "-0-" 表示）
    if day.day + 7 > _days_in_month(day.year, month):
        return _WEEK_FESTIVAL.get("%d-0-%d" % (month, weekday))
    return None


def festival(day, lunar):
    """优先返回节日名：农历传统节日 > 除夕 > 公历节日 > 寒食节。"""
    if lunar is None:
        return None

    key = "%d-%d" % (lunar.signed_month, lunar.day)
    name = LUNAR_FESTIVAL.get(key)
    if name:
        return name

    if _is_last_day_of_lunar_year(lunar):
        return "除夕"

    gregorian = _GREGORIAN_FESTIVAL.get("%d-%d" % (day.month, day.day))
    if gregorian:
        return gregorian

    gregorian = gregorian_week_festival(day)
    if gregorian:
        return gregorian

    # 寒食节 = 清明前一天
    if day.month == 4:
        qingming_day = _BASE32.index(SOLAR_TERM_DATA[day.year][6]) + 1
        if day.day == qingming_day - 1:
            return "寒食节"
    return None


def day_label(day, lunar, show_festival=True, show_term=True):
    """格子下方那行小字。"""
    if lunar is None:
        return ""
    if show_festival:
        name = festival(day, lunar)
        if name:
            return name
    if show_term:
        name = solar_term(day)
        if name:
            return name
    return lunar.day_text


def lunar_header_text(day, lunar):
    """标题区的农历总览，例如「丙午马年 十月初六」。"""
    if lunar is None:
        return ""
    return "%s%s年 %s%s" % (
        year_ganzhi(lunar.year),
        year_zodiac(lunar.year),
        lunar.month_text,
        lunar.day_text,
    )


def lunar_full_text(day, lunar):
    """完整农历，例如「农历丙午马年 十月初六」。"""
    if lunar is None:
        return ""
    return "农历%s%s年 %s%s" % (
        year_ganzhi(lunar.year),
        year_zodiac(lunar.year),
        lunar.month_text,
        lunar.day_text,
    )
