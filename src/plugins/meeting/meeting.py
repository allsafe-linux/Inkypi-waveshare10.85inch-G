import os
import logging
from datetime import datetime, timedelta

import pytz
import requests
import icalendar
import recurring_ical_events

from plugins.base_plugin.base_plugin import BasePlugin
from utils.app_utils import resolve_path

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 视图
VIEW_ROOM = "room"      # 门牌：当前会议 + 下一个会议
VIEW_AGENDA = "agenda"  # 议程：未来若干天的会议列表
VIEW_OPTIONS = [
    (VIEW_ROOM, "门牌 (Room Status)"),
    (VIEW_AGENDA, "议程 (Agenda)"),
]

# ------------------------------------------------------------------ 字体
# 官方全局 FONT_FAMILIES 只有 4 个西文字体，中文必须插件自己声明 @font-face。
# 这里用 resolve_path 生成绝对路径（src/static/fonts/xxx.ttf），
# 避免相对路径少一级导致 404。
FONT_FAMILIES = {
    "auto": ("默认 (Jost)", None),
    "ChineseHei": ("黑体", "simhei.ttf"),
    "ChineseKai": ("楷体", "simkai.ttf"),
    "ChineseFang": ("仿宋", "simfang.ttf"),
    "ChineseDaHei": ("大黑 (方正)", "方正大黑_GBK.ttf"),
    "ChineseSong": ("小标宋 (方正)", "方正小标宋_GBK.ttf"),
    "ChineseWei": ("魏碑 (方正)", "方正魏碑_GBK.ttf"),
    "ChineseKaTong": ("卡通 (方正)", "方正卡通简体.ttf"),
    "ChineseKaiGB": ("楷体 GB", "楷体_GB2312.ttf"),
    "ChineseFangGB": ("仿宋 GB", "仿宋_GB2312.ttf"),
    "Jost": ("Jost", "Jost.ttf"),
    "Napoli": ("Napoli", "Napoli.ttf"),
    "Dogica": ("Dogica", "dogicapixel.ttf"),
    "DS-Digital": ("DS-Digital", os.path.join("DS-DIGI", "DS-DIGI.TTF")),
}

# 面板四色（和官方驱动 getbuffer() 里的调色板完全一致）
PANEL_COLORS = {
    "black": "#000000",
    "white": "#FFFFFF",
    "yellow": "#FFFF00",
    "red": "#FF0000",
}

# 对比度过低、会糊成一片的组合（网页端会提示）
LOW_CONTRAST = {
    ("black", "black"),
    ("white", "white"),
    ("yellow", "yellow"),
    ("red", "red"),
    ("yellow", "white"),
    ("white", "yellow"),
}

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class Meeting(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['style_settings'] = True
        template_params['view_options'] = VIEW_OPTIONS
        template_params['font_options'] = [(k, v[0]) for k, v in FONT_FAMILIES.items()]
        template_params['color_options'] = [(k, PANEL_COLORS[k]) for k in ("black", "white", "yellow", "red")]
        return template_params

    # ------------------------------------------------------------------ ICS
    @staticmethod
    def _fetch_calendar(url):
        if url.startswith("webcal://"):
            url = url.replace("webcal://", "https://", 1)
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        # ★ 用 response.content（bytes）而不是 response.text：
        #   ICS 通常没有 charset 头，requests 会按 ISO-8859-1 解码，
        #   中文摘要会变成乱码（"产品评审会" -> "ä°å…")。
        #   交给 icalendar 自己按字节解析，它会正确识别 UTF-8。
        return icalendar.Calendar.from_ical(response.content)

    def _parse_event(self, event, tz):
        try:
            dtstart = event.decoded("dtstart")
        except Exception:
            return None

        all_day = not isinstance(dtstart, datetime)
        if all_day:
            start = tz.localize(datetime.combine(dtstart, datetime.min.time()))
        else:
            start = dtstart.astimezone(tz) if dtstart.tzinfo else tz.localize(dtstart)

        end = None
        if "dtend" in event:
            dtend = event.decoded("dtend")
            if isinstance(dtend, datetime):
                end = dtend.astimezone(tz) if dtend.tzinfo else tz.localize(dtend)
            else:
                end = tz.localize(datetime.combine(dtend, datetime.min.time()))
        elif "duration" in event:
            try:
                end = start + event.decoded("duration")
            except Exception:
                end = None

        summary = event.get("summary")
        location = event.get("location")
        return {
            "title": str(summary) if summary else "(无标题)",
            "start": start,
            "end": end,
            "location": str(location).strip() if location else "",
            "all_day": all_day,
        }

    def _fetch_events(self, urls, tz, start, end):
        parsed = []
        for url in urls:
            try:
                cal = self._fetch_calendar(url)
            except Exception as e:
                logger.warning(f"Failed to fetch ICS url {url}: {e}")
                continue
            try:
                occurrences = recurring_ical_events.of(cal).between(start, end)
            except Exception as e:
                logger.warning(f"Failed to expand ICS url {url}: {e}")
                continue
            for occurrence in occurrences:
                item = self._parse_event(occurrence, tz)
                if item:
                    parsed.append(item)
        parsed.sort(key=lambda e: (e["start"], e["title"]))
        return parsed

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _fmt_time(dt, time_format):
        if dt is None:
            return ""
        if time_format == "12h":
            return dt.strftime("%I:%M %p").lstrip("0")
        return dt.strftime("%H:%M")

    @staticmethod
    def _fmt_day(dt):
        return f"{dt.month}月{dt.day}日 {WEEKDAY_ZH[dt.weekday()]}"

    @staticmethod
    def _fmt_range(event, time_format):
        if event.get("all_day"):
            return "全天"
        start = Meeting._fmt_time(event["start"], time_format)
        end = Meeting._fmt_time(event["end"], time_format)
        return f"{start} - {end}" if end else start

    @staticmethod
    def _parse_int(value, default, low, high):
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    @staticmethod
    def _pick_color(value, default="black"):
        value = str(value or "").strip()
        return value if value in PANEL_COLORS else default

    # ------------------------------------------------------------------ 渲染
    def generate_image(self, settings, device_config):
        settings = settings if isinstance(settings, dict) else {}

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        tz_name = device_config.get_config("timezone", default="Asia/Shanghai")
        time_format = device_config.get_config("time_format", default="24h")
        try:
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = pytz.timezone("Asia/Shanghai")

        now = datetime.now(tz)

        view = str(settings.get("viewMode") or VIEW_ROOM).strip()
        if view not in (VIEW_ROOM, VIEW_AGENDA):
            view = VIEW_ROOM

        days_ahead = self._parse_int(settings.get("daysAhead"), 1, 1, 14)
        max_events = self._parse_int(settings.get("maxEvents"), 6, 1, 30)
        show_location = str(settings.get("showLocation", "true")).lower() in ("true", "1", "on", "yes")

        urls = [str(u).strip() for u in (settings.get("calendarURLs[]") or []) if str(u).strip()]
        fetch_error = ""
        events = []
        if urls:
            window_start = now - timedelta(hours=12)
            window_end = now + timedelta(days=days_ahead + 1)
            try:
                events = self._fetch_events(urls, tz, window_start, window_end)
            except Exception as e:
                logger.error(f"Failed to fetch calendar events: {e}")
                fetch_error = str(e)
        else:
            fetch_error = "未配置日历地址"

        font_key = str(settings.get("fontKey") or "auto").strip()
        if font_key not in FONT_FAMILIES:
            font_key = "auto"

        bg_key = self._pick_color(settings.get("bgColor"), "white")
        text_key = self._pick_color(settings.get("textColor"), "black")
        if (bg_key, text_key) in LOW_CONTRAST:
            # 低对比度组合在墨水屏上会糊成一片，自动换成高对比的
            text_key = "white" if bg_key == "black" else "black"

        context = {
            "view": view,
            "now": now,
            "now_text": self._fmt_time(now, time_format),
            "now_date_text": self._fmt_day(now),
            "room_name": str(settings.get("roomName") or "").strip(),
            "time_format": time_format,
            "show_location": show_location,
            "days_ahead": days_ahead,
            "events": events,
            "fetch_error": fetch_error,
            "font_key": font_key,
            "font_family": "" if font_key == "auto" else font_key,
            "bg_color_hex": PANEL_COLORS[bg_key],
            "text_color_hex": PANEL_COLORS[text_key],
            "accent_hex": PANEL_COLORS["red"],
            "accent_text_hex": PANEL_COLORS["white"],
            "low_contrast": (bg_key, text_key) in LOW_CONTRAST,
        }

        if view == VIEW_ROOM:
            context.update(self._build_room_context(context, now, time_format))
        else:
            context.update(self._build_agenda_context(context, now, time_format, days_ahead, max_events))

        extra_fonts = []
        for key, (_label, filename) in FONT_FAMILIES.items():
            if filename is None:
                continue
            extra_fonts.append({
                "family": key,
                "url": resolve_path(os.path.join("static", "fonts", filename)),
            })

        template_params = dict(context)
        template_params["extra_fonts"] = extra_fonts
        template_params["plugin_settings"] = settings

        image = self.render_image(dimensions, "meeting.html", "meeting.css", template_params)
        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        return image

    # ------------------------------------------------------------ 门牌视图
    @staticmethod
    def _build_room_context(context, now, time_format):
        events = context["events"]

        current = None
        upcoming = []
        for event in events:
            start = event["start"]
            end = event.get("end")
            if start <= now and (end is None or now < end):
                current = event
            elif start > now:
                upcoming.append(event)

        upcoming.sort(key=lambda e: e["start"])
        next_event = upcoming[0] if upcoming else None

        ctx = {
            "current_event": current,
            "next_event": next_event,
            "status_text": "进行中" if current else "空闲中",
            "status_bg_hex": PANEL_COLORS["red"] if current else PANEL_COLORS["black"],
            "status_text_hex": PANEL_COLORS["white"],
        }
        if current:
            ctx["current_time_text"] = Meeting._fmt_range(current, time_format)
            ctx["current_location"] = current["location"] if context["show_location"] else ""
        else:
            ctx["current_time_text"] = ""
            ctx["current_location"] = ""

        if next_event:
            ctx["next_time_text"] = Meeting._fmt_range(next_event, time_format)
            ctx["next_title"] = next_event["title"]
            ctx["next_location"] = next_event["location"] if context["show_location"] else ""
            ctx["next_is_today"] = next_event["start"].date() == now.date()
            ctx["next_day_text"] = Meeting._fmt_day(next_event["start"])
        else:
            ctx["next_time_text"] = ""
            ctx["next_title"] = ""
            ctx["next_location"] = ""
            ctx["next_is_today"] = False
            ctx["next_day_text"] = ""

        # 正在进行中的会议还剩多久
        if current and current.get("end"):
            remaining = current["end"] - now
            minutes = max(int(remaining.total_seconds() // 60), 0)
            if minutes >= 60:
                ctx["remaining_text"] = f"还剩 {minutes // 60} 小时 {minutes % 60} 分"
            else:
                ctx["remaining_text"] = f"还剩 {minutes} 分钟"
        else:
            ctx["remaining_text"] = ""
        return ctx

    # ------------------------------------------------------------ 议程视图
    @staticmethod
    def _build_agenda_context(context, now, time_format, days_ahead, max_events):
        events = context["events"]
        today = now.date()
        horizon = today + timedelta(days=days_ahead)

        selected = [e for e in events if today <= e["start"].date() <= horizon]
        shown = selected[:max_events]

        rows = []
        for event in shown:
            rows.append({
                "time_text": Meeting._fmt_range(event, time_format),
                "title": event["title"],
                "location": event["location"] if context["show_location"] else "",
                "day_text": Meeting._fmt_day(event["start"]),
                "is_today": event["start"].date() == today,
                "is_running": event["start"] <= now and (event.get("end") is None or now < event["end"]),
                "all_day": event.get("all_day", False),
            })

        ctx = {
            "agenda_rows": rows,
            "agenda_total": len(selected),
            "agenda_hidden": max(len(selected) - len(shown), 0),
            "agenda_empty": len(selected) == 0,
            "agenda_range_text": Meeting._fmt_day(now) if days_ahead <= 1 else
                                f"{Meeting._fmt_day(now)} 起 {days_ahead} 天",
        }
        return ctx
