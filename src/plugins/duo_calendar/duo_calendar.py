import calendar as calendar_module
import io
import os
from datetime import datetime, timedelta

import pytz
import requests
import icalendar
import recurring_ical_events
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from plugins.base_plugin.base_plugin import BasePlugin
from utils.app_utils import get_font, resolve_path

try:
    from plugins.duo_calendar import lunar as lunar_calendar
except ImportError:  # 直接以脚本方式运行时（开发期校验）
    import lunar as lunar_calendar  # type: ignore


# ------------------------------------------------------------------ 语言
# 原版社区插件只有 8 种西语/欧语语言，这里补上中文。
LOCALE_DATA = {
    "zh": {
        "weekday_abbrev": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
        "headers": ["一", "二", "三", "四", "五", "六", "日"],
        "months": ["一月", "二月", "三月", "四月", "五月", "六月",
                   "七月", "八月", "九月", "十月", "十一月", "十二月"],
        # 中文必须用中文字体画，Jost 没有 CJK 字形，用它画中文是一片空白
        "font_file": "方正大黑_GBK.ttf",
    },
    "de": {
        "weekday_abbrev": ["MO", "DI", "MI", "DO", "FR", "SA", "SO"],
        "headers": ["S", "M", "D", "M", "D", "F", "S"],
        "months": ["JANUAR", "FEBRUAR", "MÄRZ", "APRIL", "MAI", "JUNI", "JULI", "AUGUST", "SEPTEMBER", "OKTOBER", "NOVEMBER", "DEZEMBER"],
    },
    "en": {
        "weekday_abbrev": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
        "headers": ["S", "M", "T", "W", "T", "F", "S"],
        "months": ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"],
    },
    "es": {
        "weekday_abbrev": ["LUN", "MAR", "MIE", "JUE", "VIE", "SÁB", "DOM"],
        "headers": ["D", "L", "M", "M", "J", "S", "D"],
        "months": ["ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO", "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE"],
    },
    "fr": {
        "weekday_abbrev": ["LUN", "MAR", "MER", "JEU", "VEN", "SAM", "DIM"],
        "headers": ["D", "L", "M", "M", "J", "S", "D"],
        "months": ["JANVIER", "FÉVRIER", "MARS", "AVRIL", "MAI", "JUIN", "JUILLET", "AOÛT", "SEPTEMBRE", "OCTOBRE", "NOVEMBRE", "DÉCEMBRE"],
    },
    "id": {
        "weekday_abbrev": ["SEN", "SEL", "RAB", "KAM", "JUM", "SAB", "MIN"],
        "headers": ["M", "S", "S", "R", "K", "J", "S"],
        "months": ["JANUARI", "FEBRUARI", "MARET", "APRIL", "MEI", "JUNI", "JULI", "AGUSTUS", "SEPTEMBER", "OKTOBER", "NOVEMBER", "DESEMBER"],
    },
    "it": {
        "weekday_abbrev": ["LUN", "MAR", "MER", "GIO", "VEN", "SAB", "DOM"],
        "headers": ["D", "L", "M", "M", "G", "S", "D"],
        "months": ["GENNAIO", "FEBBRAIO", "MARZO", "APRILE", "MAGGIO", "GIUGNO", "LUGLIO", "AGOSTO", "SETTEMBRE", "OTTOBRE", "NOVEMBRE", "DICEMBRE"],
    },
    "nl": {
        "weekday_abbrev": ["MAA", "DIN", "WOE", "DON", "VRI", "ZAT", "ZON"],
        "headers": ["Z", "M", "D", "W", "D", "V", "Z"],
        "months": ["JANUARI", "FEBRUARI", "MAART", "APRIL", "MEI", "JUNI", "JULI", "AUGUSTUS", "SEPTEMBER", "OKTOBER", "NOVEMBER", "DECEMBER"],
    },
    "pt": {
        "weekday_abbrev": ["SEG", "TER", "QUA", "QUI", "SEX", "SÁB", "DOM"],
        "headers": ["D", "S", "T", "Q", "Q", "S", "D"],
        "months": ["JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO", "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO"],
    },
}

LANGUAGE_OPTIONS = (
    ("zh", "中文"),
    ("nl", "荷兰语"),
    ("en", "英语"),
    ("fr", "法语"),
    ("de", "德语"),
    ("id", "印尼语"),
    ("it", "意大利语"),
    ("pt", "葡萄牙语"),
    ("es", "西班牙语"),
)

# ------------------------------------------------------------------ 面板四色
# 微雪 10.85" (G) 只有 4 色：黑 / 白 / 黄 / 红。
#
# ★ 配色铁律（2026-11-14 重新设计）：
#   黄色 (255,255,0) 在白底上的对比度只有约 1.07:1，**几乎看不清**。
#   所以黄色只允许出现在**黑色底块上**（黄字黑底 = 21:1，清晰）。
#   白底上的所有文字一律黑色（21:1）或红色（约 4.7:1）。
PANEL_COLORS = {
    "black": "#000000",
    "white": "#FFFFFF",
    "yellow": "#FFFF00",
    "red": "#FF0000",
}

PAPER = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (255, 0, 0)
YELLOW = (255, 255, 0)

# 三个语义色（对应设置页的三个下拉框）
#   primary  = 正文色（日期数字、农历小字、待办正文）-> 默认黑
#   accent   = 强调色（月份名、节日、节气）        -> 默认红
#   band     = 黑底色块上的字色（星期条、今天标记、待办标题）-> 默认黄
DEFAULT_PRIMARY = "black"
DEFAULT_ACCENT = "red"
DEFAULT_BAND = "yellow"

# 兼容旧设置名
DEFAULT_HIGHLIGHT = DEFAULT_ACCENT
DEFAULT_MUTED = DEFAULT_BAND

# 版面模式
LAYOUT_MODES = (
    ("split", "左万年历 + 右待办"),
    ("full", "全屏万年历"),
)
DEFAULT_LAYOUT = "split"

# 在线背景图强度：四色墨水屏上照片会被抖成黑白点阵，
# 强度越大越接近原图，但正文会越难读。默认「淡」。
BACKGROUND_STRENGTHS = (
    ("0", "关闭"),
    ("1", "淡（推荐）"),
    ("2", "中"),
    ("3", "原图（满幅）"),
)
DEFAULT_BACKGROUND_STRENGTH = "1"
BACKGROUND_MAX_BYTES = 12 * 1024 * 1024

# 中文字体优先级：粗黑体在墨水屏上笔画最实
CJK_FONT_FILES = ("方正大黑_GBK.ttf", "simhei.ttf", "黑体.ttf", "simkai.ttf", "simfang.ttf")


class DuoCalendar(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        template_params["language_options"] = LANGUAGE_OPTIONS
        template_params["color_options"] = [
            ("black", "黑色"),
            ("white", "白色"),
            ("yellow", "黄色"),
            ("red", "红色"),
        ]
        template_params["default_primary"] = DEFAULT_PRIMARY
        template_params["default_highlight"] = DEFAULT_ACCENT
        template_params["default_muted"] = DEFAULT_BAND
        template_params["layout_modes"] = LAYOUT_MODES
        template_params["default_layout"] = DEFAULT_LAYOUT
        template_params["background_strengths"] = BACKGROUND_STRENGTHS
        template_params["default_background_strength"] = DEFAULT_BACKGROUND_STRENGTH
        return template_params

    def generate_image(self, settings, device_config):
        settings = settings if isinstance(settings, dict) else {}

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        timezone_name = device_config.get_config("timezone", default="Asia/Shanghai")
        time_format = device_config.get_config("time_format", default="12h")
        try:
            timezone = pytz.timezone(timezone_name)
        except (pytz.UnknownTimeZoneError, AttributeError):
            timezone = pytz.timezone("Asia/Shanghai")

        current_datetime = datetime.now(timezone)
        selected_date = self._get_selected_date(settings, current_datetime)
        language = self._get_locale_key(settings.get("language") or settings.get("locale", "en"))

        primary = self._parse_color(settings.get("primaryColor"), PANEL_COLORS[DEFAULT_PRIMARY])
        accent = self._parse_color(
            settings.get("highlightColor", settings.get("accentColor")), PANEL_COLORS[DEFAULT_ACCENT]
        )
        band = self._parse_color(
            settings.get("mutedColor", settings.get("bandColor")), PANEL_COLORS[DEFAULT_BAND]
        )

        # 兜底 1：正文/强调色画在白底上，同色 = 隐形 -> 强制换成黑色
        primary = self._ensure_visible(primary)
        accent = self._ensure_visible(accent)
        # 兜底 2：色块字画在黑底上，和底色同色 = 隐形 -> 强制换黄色
        band = self._ensure_visible_on_black(band, primary)

        show_lunar = self._parse_boolean(settings.get("showLunar"), default=True)
        show_festivals = self._parse_boolean(settings.get("showFestivals"), default=True)
        layout_mode = self._get_layout_mode(settings.get("layoutMode"))

        # 在线背景图（拉不到就安静地用白底，不影响日历）
        background_strength = self._get_background_strength(settings.get("backgroundStrength"))
        background = None
        if background_strength > 0:
            background = self._fetch_background(
                settings.get("backgroundImageURL"), dimensions, background_strength
            )

        # 关联的 To-Do 列表
        todo = self._resolve_todo(settings, device_config)

        return self._render_calendar(
            dimensions,
            selected_date,
            current_datetime,
            time_format,
            LOCALE_DATA[language],
            primary,
            accent,
            band,
            show_lunar,
            show_festivals,
            layout_mode,
            todo,
            background,
        )

    # ---------------------------------------------------------- 在线背景图
    @staticmethod
    def _get_background_strength(value):
        text = str(value if value is not None else DEFAULT_BACKGROUND_STRENGTH).strip()
        if text in ("0", "1", "2", "3"):
            return int(text)
        return int(DEFAULT_BACKGROUND_STRENGTH)

    @staticmethod
    def _fetch_background(url, dimensions, strength):
        """拉一张在线图片，处理成墨水屏能显示的点阵底图。

        四色屏只有黑白红黄，照片必须先转灰度、再抖动成黑白点阵。
        强度越大越接近原图，但正文会越难读 —— 所以默认只取很淡的一层。
        拉取失败一律返回 None（白底），不影响日历显示。
        """
        url = str(url or "").strip()
        if not url:
            return None
        if url.startswith("webcal://"):
            return None
        try:
            response = requests.get(url, timeout=20, stream=True)
            response.raise_for_status()
            data = b""
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                data += chunk
                if len(data) >= BACKGROUND_MAX_BYTES:
                    break
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception:
            return None

        try:
            gray = image.convert("L")
            gray = ImageOps.fit(
                gray, dimensions, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
            )
            # 提亮 -> 只剩暗部变成黑点，强度越大偏置越小
            bias = {1: 150, 2: 90, 3: 20}.get(strength, 150)
            gray = gray.point(lambda value: max(0, min(255, value + bias)))
            dithered = gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
            return dithered.convert("RGB")
        except Exception:
            return None

    # ---------------------------------------------------------- 关联待办列表
    @staticmethod
    def _resolve_todo(settings, device_config):
        """取「关联的 To-Do 列表」。

        优先从播放列表里的 todo_list 插件实例读第 N 条清单；
        读不到就退回设置页里手填的文本。
        返回 (标题, [条目...]) 或 None。
        """
        text = str(settings.get("todoText") or "").strip()
        if text:
            title = str(settings.get("todoTitle") or "待办").strip() or "待办"
            return title, [line.strip() for line in text.split("\n") if line.strip()]

        try:
            index = int(str(settings.get("todoSource") or "1").strip() or "1")
        except ValueError:
            index = 1
        if index <= 0:
            return None

        try:
            manager = device_config.get_playlist_manager()
        except Exception:
            return None

        for playlist in manager.playlists:
            for instance in getattr(playlist, "plugins", []):
                if instance.plugin_id != "todo_list":
                    continue
                instance_settings = instance.settings or {}
                bodies = instance_settings.get("list[]", []) or []
                titles = instance_settings.get("list-title[]", []) or []
                if isinstance(bodies, str):
                    bodies = [bodies]
                if isinstance(titles, str):
                    titles = [titles]
                if index > len(bodies):
                    continue
                items = [line.strip() for line in str(bodies[index - 1]).split("\n") if line.strip()]
                title = str(titles[index - 1]).strip() if index <= len(titles) else ""
                return title or "待办", items
        return None

    @staticmethod
    def _get_layout_mode(value):
        value = str(value or DEFAULT_LAYOUT).strip().lower()
        return value if value in dict(LAYOUT_MODES) else DEFAULT_LAYOUT

    # -------------------------------------------------------------- 渲染
    def _render_calendar(
        self,
        dimensions,
        selected_date,
        current_datetime,
        time_format,
        locale_data,
        primary,
        accent,
        band,
        show_lunar,
        show_festivals,
        layout_mode,
        todo,
        background=None,
    ):
        width, height = dimensions
        if background is not None:
            image = background.convert("RGB")
            if image.size != dimensions:
                image = image.resize(dimensions, Image.Resampling.LANCZOS)
        else:
            image = Image.new("RGB", dimensions, PAPER)
        draw = ImageDraw.Draw(image)

        is_portrait = width < height
        split = (layout_mode == "split") and not is_portrait

        pad = max(int(width * 0.012), 10)
        if split:
            todo_width = int(width * 0.30)
            divider_width = 4
        else:
            todo_width = 0
            divider_width = 0
        calendar_right = width - todo_width - divider_width

        # ---------------------------------------------------------- 字体
        title_height = max(int(height * 0.115), 44)
        weekday_height = max(int(height * 0.050), 20)
        rule_height = 3
        grid_top = title_height + weekday_height + rule_height
        grid_bottom = height - max(int(height * 0.016), 6)

        rows = 6
        row_height = (grid_bottom - grid_top) / float(rows)

        day_size = max(int(row_height * 0.495), 18)
        label_size = max(int(row_height * 0.31), 12)
        month_size = max(int(height * 0.095), 30)
        year_size = max(int(height * 0.050), 16)
        time_size = max(int(height * 0.070), 22)
        lunar_header_size = max(int(height * 0.038), 13)
        weekday_size = max(int(height * 0.046), 15)

        month_font = self._get_locale_font(locale_data, month_size)
        year_font = self._get_locale_font(locale_data, year_size)
        time_font = get_font("Jost", time_size)
        lunar_header_font = self._get_cjk_font(lunar_header_size)
        weekday_font = self._get_cjk_font(weekday_size)
        day_font = get_font("Jost", day_size, "bold")
        label_font = self._get_cjk_font(label_size)

        # ---------------------------------------------------------- 标题区
        # 农历总览并到标题行上（不再单独占一行），省下的高度给网格，字才能更大。
        title_baseline = int(title_height * 0.80)
        month_name = locale_data["months"][selected_date.month - 1].capitalize()
        self._draw_text(draw, (pad, title_baseline), month_name, month_font, accent, anchor="ls")
        cursor_x = pad + draw.textlength(month_name, font=month_font) + 12

        year_text = "%d年" % selected_date.year
        self._draw_text(draw, (cursor_x, title_baseline), year_text, year_font, primary, anchor="ls")
        cursor_x += draw.textlength(year_text, font=year_font) + 14

        today_lunar = lunar_calendar.solar_to_lunar(current_datetime.date())
        if show_lunar and today_lunar is not None:
            header_text = lunar_calendar.lunar_header_text(current_datetime.date(), today_lunar)
            self._draw_text(
                draw, (cursor_x, title_baseline), "· " + header_text,
                lunar_header_font, primary, anchor="ls",
            )

        time_text = current_datetime.strftime("%H:%M" if time_format == "24h" else "%I:%M %p")
        self._draw_text(
            draw, (calendar_right - pad, title_baseline), time_text, time_font, primary, anchor="rs"
        )

        # ---------------------------------------------------------- 星期行（黑字 + 细横线）
        # 不用黑底色块：省下的 30px 全部给网格，日期和农历字能再放大一号。
        # 黄色仍然只出现在黑底上（今天的黑圆、待办标题条）。
        for index, label in enumerate(locale_data["headers"]):
            center_x = self._column_center(pad, calendar_right - 2 * pad, index)
            self._draw_centered(
                draw,
                (center_x, title_height + weekday_height / 2.0),
                label,
                weekday_font,
                primary,
            )
        draw.rectangle(
            (pad, title_height + weekday_height, calendar_right - pad, title_height + weekday_height + rule_height),
            fill=primary,
        )

        # ---------------------------------------------------------- 网格
        grid_width = calendar_right - 2 * pad
        column_width = grid_width / 7.0
        month_grid = calendar_module.Calendar(firstweekday=6).monthdatescalendar(
            selected_date.year, selected_date.month
        )
        while len(month_grid) < rows:
            next_week_start = month_grid[-1][-1] + timedelta(days=1)
            month_grid.append(
                [next_week_start + timedelta(days=day_offset) for day_offset in range(7)]
            )

        # ★ 固定行盒：每个格子 = 上留白 + 日期行 + 中间隙 + 小字行 + 下留白
        #   三段留白严格相等 -> 所有内容行距相同，长短内容互不影响。
        day_slot = day_size * 0.74
        label_slot = label_size * 1.02
        gap = (row_height - day_slot - label_slot) / 3.0
        if gap < 2:
            gap = 2.0
            day_slot = (row_height - 2 * gap - label_slot) * 0.5

        for week_index, week in enumerate(month_grid):
            row_top = grid_top + row_height * week_index
            day_center_y = row_top + gap + day_slot / 2.0
            label_center_y = row_top + gap + day_slot + gap + label_slot / 2.0

            for weekday_index, day in enumerate(week):
                center_x = self._column_center(pad, grid_width, weekday_index)
                is_outside_month = day.month != selected_date.month
                is_selected = day == selected_date

                # ---- 农历 / 节日 / 节气（节日 > 节气 > 农历日）
                label_text = ""
                label_color = primary
                if show_lunar:
                    lunar = lunar_calendar.solar_to_lunar(day)
                    if lunar is not None:
                        festival_name = lunar_calendar.festival(day, lunar) if show_festivals else None
                        term_name = lunar_calendar.solar_term(day) if show_festivals else None
                        if festival_name:
                            label_text = festival_name
                            label_color = accent
                        elif term_name:
                            label_text = term_name
                            label_color = accent
                        else:
                            label_text = lunar.day_text
                            label_color = primary

                # ---- 日期数字
                day_text = str(day.day)
                day_color = primary
                day_font_used = day_font
                if is_outside_month:
                    # 不是本月 -> 画小一号、不带农历小字，视觉上自然退到背景
                    day_font_used = get_font("Jost", max(int(day_size * 0.72), 12), "bold")
                elif is_selected:
                    # 今天：黑底黄字（黄字只在黑底上用），圆圈按数字实际大小算
                    bbox = draw.textbbox((0, 0), day_text, font=day_font, anchor="mm")
                    circle_radius = int(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 2.0) + 4
                    draw.ellipse(
                        (
                            center_x - circle_radius,
                            day_center_y - circle_radius,
                            center_x + circle_radius,
                            day_center_y + circle_radius,
                        ),
                        fill=primary,
                    )
                    day_color = band if band != primary else PAPER

                self._draw_centered(
                    draw, (center_x, day_center_y), day_text, day_font_used, day_color
                )

                if label_text and not is_outside_month:
                    self._draw_centered(
                        draw, (center_x, label_center_y), label_text, label_font, label_color
                    )

        # ---------------------------------------------------------- 右栏待办
        if split:
            draw.rectangle((calendar_right, 0, calendar_right + divider_width, height), fill=BLACK)
            self._render_todo_panel(
                draw, calendar_right + divider_width, width, height, primary, band, todo
            )

        return image

    # ---------------------------------------------------------- 待办面板
    def _render_todo_panel(self, draw, x0, x1, height, primary, band, todo):
        pad = 14
        header_height = max(int(height * 0.095), 38)

        # 标题条：黑底黄字
        draw.rectangle((x0, 0, x1, header_height), fill=BLACK)
        title = (todo[0] if todo else "待办") or "待办"
        self._draw_text(
            draw, (x0 + pad, header_height / 2.0), title, self._get_cjk_font(int(header_height * 0.52)),
            band, anchor="lm",
        )

        if not todo or not todo[1]:
            self._draw_text(
                draw,
                (x0 + pad, header_height + 28),
                "未关联待办列表" if not todo else "这一栏是空的",
                self._get_cjk_font(int(height * 0.042)),
                primary,
                anchor="lm",
            )
            return

        item_size = max(int(height * 0.048), 18)
        item_font = self._get_cjk_font(item_size)
        row_height = max(int(height * 0.082), 34)
        y = header_height + int(row_height * 0.45)

        box_size = int(item_size * 0.82)
        for text in todo[1]:
            if y + row_height > height - 6:
                break
            # 复选框：黑框（未完成）/ 实心黑（完成）
            box_x = x0 + pad
            box_y = y - box_size / 2.0
            done = text.startswith("[x]") or text.startswith("[X]")
            if done:
                draw.rectangle((box_x, box_y, box_x + box_size, box_y + box_size), fill=primary)
                content = text[3:].strip()
            else:
                draw.rectangle(
                    (box_x, box_y, box_x + box_size, box_y + box_size), outline=primary, width=3
                )
                content = text[4:].strip() if text.startswith("[ ]") else text

            if content:
                text_w = draw.textlength(content, font=item_font)
                self._draw_text(
                    draw,
                    (box_x + box_size + 12, y),
                    content,
                    item_font,
                    primary,
                    anchor="lm",
                )
                if done:
                    # 已完成：划一条删除线，四色屏上也能一眼看出做完没
                    line_y = y - item_size * 0.08
                    draw.line(
                        (
                            box_x + box_size + 12,
                            line_y,
                            box_x + box_size + 12 + text_w,
                            line_y,
                        ),
                        fill=primary,
                        width=2,
                    )
            y += row_height

    # -------------------------------------------------------------- 绘制辅助
    @staticmethod
    def _draw_text(draw, xy, text, font, fill, anchor="mm"):
        """按 anchor 直接绘制（"ls"/"rs" 是基线对齐，"lm" 是左边垂直居中）。"""
        draw.text(xy, text, font=font, fill=fill, anchor=anchor)

    @staticmethod
    def _draw_centered(draw, xy, text, font, fill):
        """把文字的**墨迹中心**精确放到 xy。

        Pillow 的 "mm" 锚点是按字体 ascender/descender 居中的，
        数字和汉字的视觉中心会差几个像素；这里先量墨迹包围盒再微调，
        保证每一行都落在设计好的槽位上，行距才不会忽宽忽窄。
        """
        bbox = draw.textbbox((0, 0), text, font=font, anchor="mm")
        offset = (bbox[1] + bbox[3]) / 2.0
        x, y = xy
        draw.text((x, y - offset), text, font=font, fill=fill, anchor="mm")

    @staticmethod
    def _column_center(side_padding, grid_width, index):
        column_width = grid_width / 7.0
        return side_padding + column_width * (index + 0.5)

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _get_locale_font(locale_data, font_size):
        """按语言选字体：中文用中文字体，其余用 Jost。

        Jost 没有 CJK 字形，用它画中文会画出一片空白。
        """
        font_file = locale_data.get("font_file")
        if font_file:
            try:
                return ImageFont.truetype(
                    resolve_path(os.path.join("static", "fonts", font_file)), font_size
                )
            except OSError:
                pass
        return get_font("Jost", font_size)

    @staticmethod
    def _get_cjk_font(font_size):
        """农历/节日这类中文小字统一用中文字体（粗黑体优先）。"""
        for name in CJK_FONT_FILES:
            try:
                return ImageFont.truetype(
                    resolve_path(os.path.join("static", "fonts", name)), font_size
                )
            except OSError:
                continue
        return get_font("Jost", font_size)

    @staticmethod
    def _get_selected_date(settings, current_datetime):
        custom_date = settings.get("customDate")
        if not custom_date:
            return current_datetime.date()

        try:
            return datetime.strptime(str(custom_date), "%Y-%m-%d").date()
        except ValueError as error:
            raise RuntimeError("Invalid date. Use YYYY-MM-DD.") from error

    @staticmethod
    def _get_locale_key(language):
        language = str(language or "en").strip().lower()
        return language if language in LOCALE_DATA else "en"

    @staticmethod
    def _parse_boolean(value, default=False):
        if value is None or value == "":
            return default
        return str(value).strip().lower() in {"true", "1", "on", "yes"}

    @staticmethod
    def _parse_color(value, fallback):
        """接受面板四色的名字（black/white/yellow/red）或 #RRGGBB。"""
        if not value:
            return fallback
        text = str(value).strip()
        if text.lower() in PANEL_COLORS:
            return ImageColor.getrgb(PANEL_COLORS[text.lower()])[:3]
        try:
            return ImageColor.getrgb(text)[:3]
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _ensure_visible(color):
        """白底上同色 = 隐形，强制换成黑色。

        这个坑真实发生过：高亮色被设成白色后，月份名、时间、巨大数字
        整片消失，日历看起来「没有标题」。
        """
        if tuple(color[:3]) == PAPER:
            return BLACK
        return tuple(color[:3])

    @staticmethod
    def _ensure_visible_on_black(color, background):
        """黑底色块上同色 = 隐形，强制换成黄色。"""
        if tuple(color[:3]) == tuple(background[:3]):
            return YELLOW
        return tuple(color[:3])
