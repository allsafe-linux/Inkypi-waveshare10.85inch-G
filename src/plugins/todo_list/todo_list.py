from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image
from io import BytesIO
import requests
import logging
import math
import os

from utils.app_utils import resolve_path

logger = logging.getLogger(__name__)

FONT_SIZES = {
    "x-small": 0.7,
    "small": 0.9,
    "normal": 1,
    "large": 1.1,
    "x-large": 1.3
}

# 每栏的目标宽度（px）。1360 宽的 10.85 寸屏 -> 1360 // 340 = 4 栏
TARGET_COLUMN_WIDTH = 340
# 栏数上限
MAX_COLUMNS = 8

# 版面常量（要和 render/todo_list.css 对齐，只用于估算，估歪了不影响正确性）
BODY_MARGIN = 5          # px，plugin.html 的 body margin
BODY_PADDING_VW = 1.5    # vw，plugin.html 的 body padding
FRAME_BORDER_VW = 0.7    # vw，Rectangle 边框
COLUMN_GAP = 16          # px，.lists-container 的 gap
LIST_PADDING = 16        # px，.list 左右 padding
LIST_BORDER = 3          # px，.list 左右边框 1.5 * 2
ITEM_LINE_HEIGHT = 1.2   # 行高倍数
ITEM_PADDING = 6.7       # px，每条上下 padding（原来 2cqh ≈ 6.7）

# 清单被拆到多栏时，标题怎么显示：
#   'header' = 提升为整行标题，显示在所有栏上方（默认）
#   'repeat' = 每栏顶部都重复一次标题
SPLIT_TITLE_MODE = 'header'

# ---------------------------------------------------------------- 字体
# 面板只认 4 种颜色，所以字体/配色都从这几个里挑。
# 字体文件在 src/static/fonts/ 下，官方全局 FONT_FAMILIES 只有 4 个西文字体，
# 中文靠插件自己声明 @font-face（和 meeting 插件一个套路，但用绝对路径，
# 避免 meeting 插件里 ../../static/... 少一级的 bug）。
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


class TodoList(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['style_settings'] = True
        return template_params

    # ------------------------------------------------------------------ 栏数
    def _resolve_column_count(self, width, settings, list_count):
        """决定用几栏。

        设置里的 'columns'：
          'auto' -> 按屏幕宽度自动算（屏宽 // 目标栏宽），并且不小于清单数
          数字    -> 手动指定
        """
        raw = str(settings.get('columns', 'auto')).strip().lower()
        if raw.isdigit():
            count = int(raw)
        else:
            count = max(width // TARGET_COLUMN_WIDTH, list_count)
        return max(1, min(MAX_COLUMNS, count))

    # -------------------------------------------------------------- 版面估算
    def _layout_metrics(self, dimensions, settings, column_count):
        """粗算每栏的内容宽度 / 内容高度，用来分配条目（估歪了不影响正确性）。"""
        width, height = dimensions
        vw = width / 100.0

        try:
            margin = float(settings.get('margin') or BODY_MARGIN)
        except (TypeError, ValueError):
            margin = BODY_MARGIN

        frame = 2 * FRAME_BORDER_VW * vw if settings.get('selectedFrame') == 'Rectangle' else 0.0
        chrome = 2 * margin + 2 * BODY_PADDING_VW * vw + frame

        col_outer = (width - chrome - (column_count - 1) * COLUMN_GAP) / column_count
        col_content_w = max(40.0, col_outer - 2 * LIST_PADDING - LIST_BORDER)
        col_content_h = max(40.0, (height - chrome) * 0.78)
        return col_content_w, col_content_h

    @staticmethod
    def _estimate_height(text, col_content_w, font_px):
        """粗算一个条目占多高（中文按 1 个字宽，英文按半个字宽）。"""
        units = 0.0
        for ch in text:
            units += 1.0 if ord(ch) > 0x2E80 else 0.5
        text_px = units * font_px
        lines = max(1, int(math.ceil(text_px / max(1.0, col_content_w))))
        return lines * font_px * ITEM_LINE_HEIGHT + ITEM_PADDING

    # -------------------------------------------------------------- 清单样式
    @staticmethod
    def _pick(values, index, default):
        """从一组平行数组里取第 index 个值，越界或非法就用默认值。"""
        if index < len(values):
            value = values[index]
            if value is not None and str(value).strip() != '':
                return str(value).strip()
        return default

    def _build_lists(self, settings):
        """把表单里的几组平行数组拼成带样式的清单列表。"""
        titles = settings.get('list-title[]', []) or []
        bodies = settings.get('list[]', []) or []
        fonts = settings.get('list-font[]', []) or []
        text_colors = settings.get('list-text-color[]', []) or []
        bg_colors = settings.get('list-bg-color[]', []) or []

        lists = []
        for i, raw_list in enumerate(bodies):
            title = self._pick(titles, i, "")
            elements = [line for line in str(raw_list).split('\n') if line.strip()]

            font_key = self._pick(fonts, i, "auto")
            if font_key not in FONT_FAMILIES:
                font_key = "auto"

            text_color = self._pick(text_colors, i, "black")
            if text_color not in PANEL_COLORS:
                text_color = "black"

            bg_color = self._pick(bg_colors, i, "white")
            if bg_color not in PANEL_COLORS:
                bg_color = "white"

            lists.append({
                'title': title,
                'elements': elements,
                'font': font_key,
                'font_key': font_key,
                'text_color': text_color,
                'text_color_hex': PANEL_COLORS[text_color],
                'bg_color': bg_color,
                'bg_color_hex': PANEL_COLORS[bg_color],
                'low_contrast': (bg_color, text_color) in LOW_CONTRAST,
            })
        return lists

    # -------------------------------------------------------------- 分配条目
    def _distribute(self, lists, column_count, dimensions, settings):
        """把清单分配到 column_count 栏。

        规则：
          - 只有 1 个清单：条目按行高均衡分到各栏，标题提升成整行标题
          - 2 个及以上清单：每个清单独占一栏，不拆分（栏数 = min(栏数, 清单数)）

        返回 (columns, section_titles)
          columns        = [{'groups': [...], 'style': {...}}]
          section_titles = 跨栏清单的整行标题
        """
        columns = [{'groups': [], 'style': None} for _ in range(column_count)]
        if not lists:
            return columns, []

        # 清单数 >= 2：每个清单独占一栏，不拆分。
        # 拆分多清单会让「哪个条目属于哪个清单」看不清（标题和条目分家），
        # 所以只有「单个清单」才走均衡分栏那条路。
        if len(lists) >= 2:
            effective = min(column_count, len(lists))
            columns = columns[:effective]
            for i, lst in enumerate(lists):
                col_index = min(i, effective - 1)
                columns[col_index]['groups'].append({
                    'title': lst['title'],
                    'elements': lst['elements'],
                    'show_title': True,
                    'style': lst,
                })
            return columns, []

        # 只有 1 个清单：把条目按行高均衡切到各栏（清单允许跨栏）
        col_w, col_h = self._layout_metrics(dimensions, settings, column_count)
        font_px = max(10.0, col_h / 12.5)

        only = lists[0]
        flat = [(only['title'], el) for el in only['elements']]

        if not flat:
            columns[0]['groups'].append({
                'title': only['title'],
                'elements': [],
                'show_title': True,
                'style': only,
            })
            return columns, []

        heights = [self._estimate_height(el, col_w, font_px) for (_, el) in flat]
        target = sum(heights) / column_count

        buckets = [[] for _ in range(column_count)]
        cur = 0
        cur_h = 0.0
        for entry, h in zip(flat, heights):
            # 当前栏已经过半目标高度，且后面还有栏 -> 开下一栏
            if cur < column_count - 1 and cur_h > 0 and cur_h + h / 2 > target:
                cur += 1
                cur_h = 0.0
            buckets[cur].append(entry)
            cur_h += h

        for col_idx, bucket in enumerate(buckets):
            groups = []
            for list_title, element in bucket:
                if groups and groups[-1]['_title_key'] == list_title:
                    groups[-1]['elements'].append(element)
                else:
                    groups.append({
                        '_title_key': list_title,
                        'title': list_title,
                        'elements': [element],
                        'show_title': False,
                        'style': only,
                    })
            columns[col_idx]['groups'] = groups

        # 标题显示：只在一栏里出现的清单 -> 标题留在栏内；
        #           跨了多栏的清单 -> 按 SPLIT_TITLE_MODE 处理
        appearance = {}
        for col_idx, col in enumerate(columns):
            for group in col['groups']:
                appearance.setdefault(group['_title_key'], []).append(col_idx)

        section_titles = []
        for col_idx, col in enumerate(columns):
            for group in col['groups']:
                cols = appearance[group['_title_key']]
                if len(cols) == 1:
                    group['show_title'] = True
                elif SPLIT_TITLE_MODE == 'repeat':
                    group['show_title'] = True
                else:
                    group['show_title'] = False
                    if col_idx == cols[0]:
                        section_titles.append({'title': group['title'], 'style': group['style']})

        for col in columns:
            for group in col['groups']:
                group.pop('_title_key', None)

        return columns, section_titles

    # ------------------------------------------------------------------ 渲染
    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        lists = self._build_lists(settings)
        column_count = self._resolve_column_count(dimensions[0], settings, len(lists))
        columns, section_titles = self._distribute(lists, column_count, dimensions, settings)

        # 每一栏用它第一个 group 的样式（便签底色/字色/字体）
        for col in columns:
            col['style'] = col['groups'][0]['style'] if col['groups'] else None

        # 插件自己声明中文 @font-face（官方全局只有 4 个西文字体）
        extra_fonts = []
        for key, (_label, filename) in FONT_FAMILIES.items():
            if filename is None:
                continue
            extra_fonts.append({
                'family': key,
                'url': resolve_path(os.path.join("static", "fonts", filename)),
            })

        template_params = {
            "title": settings.get('title'),
            "list_style": settings.get('listStyle', 'disc'),
            "font_scale": FONT_SIZES.get(settings.get('fontSize', 'normal'), 1),
            "lists": lists,
            "columns": columns,
            "section_titles": section_titles,
            "column_count": column_count,
            "extra_fonts": extra_fonts,
            "plugin_settings": settings
        }

        image = self.render_image(dimensions, "todo_list.html", "todo_list.css", template_params)
        return image
