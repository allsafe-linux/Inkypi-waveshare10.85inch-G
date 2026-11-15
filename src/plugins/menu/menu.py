# -*- coding: utf-8 -*-
"""菜单价目表插件（咖啡店 / 小店摆台价目表用）。

特点
----
* **手动录入**：菜名 / 价格 / 介绍 / 分类，全部在设置页里直接填。
* **背景可自定义**：四种来源 —— 纯色 / 渐变 / 上传图片 / 网络图片（URL），
  外加一条**连续的背景浓度滑块**（0-100）。
  四色屏会把照片和中间色抖成黑白点阵，所以浓度默认压得很低，保证字能看清。
* **最大化屏幕利用率**：1360×480 这种超宽屏，
  - ``columns`` 版式：按「分类」自动分栏，栏数随分类数自动变化；
  - ``grid`` 版式：菜品平铺成卡片网格，条目按分类归拢，不被打散。

  两种版式的**字号都按内容自适应放大** —— 内容少就把字放大撑满，
  内容多才缩回去，不留大片空白。

渲染全程 Pillow 直绘（不走 chromium）：墨水屏只有 4 色，
直绘能精确控制每一笔落在哪 4 个颜色上，不会出现灰度抖成噪点的情况。
"""

import hashlib
import logging
import os

from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from plugins.base_plugin.base_plugin import BasePlugin
from utils.app_utils import resolve_path

logger = logging.getLogger(__name__)

# 墨水屏只有 4 色，插件里只用这 4 个颜色值（量化时不会抖成噪点）
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)

FONT_DIR = resolve_path(os.path.join("static", "fonts"))
NAME_FONT_FILE = "方正大黑_GBK.ttf"   # 菜名：粗黑体，墨水屏上笔画最实
DESC_FONT_FILE = "simhei.ttf"         # 介绍：黑体，小字也清楚

# 价格前缀用**全角**￥（U+FFE5）。半角 ¥（U+00A5）在方正大黑_GBK 里没有字形，
# 画出来是一片空白 —— 实测 black_px=0。
YUAN_SIGN = "￥"

# ★ 中文字体依赖（2026-11-15，为第三方发布加的）
#   菜名和介绍都要靠中文字体渲染，而这两个字体（方正大黑_GBK / simhei）
#   **都有版权、不能随插件分发**，上游 InkyPi 默认也不带任何中文字体。
#   第三方用户装完如果没装字体，菜名和介绍会全空白，
#   看起来像「插件坏了」，其实只是缺字体 —— 所以要主动检测并画说明图。
#
#   降级策略：
#     · 菜名字体缺失 → 没中文可画，直接画一张英文说明图；
#     · 只有介绍字体缺失 → 用菜名字体顶上（菜名字体本身也是中文字体），
#       菜单照常显示，只是介绍和菜名同字体。
#   字体文件名可以在下面两个常量里改成你机器上已有的字体。

# 字号自适应放大的上限（2.4 倍基准字号）
MAX_SCALE = 2.4

# ★ 网格版式的字号比例（都相对格子高度 / 菜名字号）。
#   小樱反馈「分类和介绍的字体太小看不清」→ 这三个是可调旋钮，
#   提成常量后可以在离线渲染里一次试出多档，不用反复改代码。
NAME_BASE_RATIO = 0.16     # 菜名基准字号 = 格高 * 这个
DESC_BASE_RATIO = 0.105    # 介绍基准字号 = 格高 * 这个
TAG_RATIO = 0.50           # 分类标签字号 = 菜名字号 * 这个
DESC_SCALE_CAP = 1.45      # 介绍相对基准字号的放大上限（再大就撑爆格子）


def _font_size(font, default=12):
    """取字号；Pillow 默认字体没有 size 属性。"""
    size = getattr(font, "size", None)
    try:
        return int(size)
    except (TypeError, ValueError):
        return default


def _font_file_ok(filename):
    """字体文件**存在**且**能被 Pillow 真正加载**（损坏的 ttf 也算不行）。"""
    path = os.path.join(FONT_DIR, filename)
    if not os.path.isfile(path):
        return False
    try:
        ImageFont.truetype(path, 20)
        return True
    except Exception:
        return False


def _load_font(filename, size):
    """取字体；取不到就退回 Pillow 默认字体，绝不让插件整个挂掉。"""
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, filename), int(size))
    except Exception:
        pass
    # ★ Pillow >= 10.1 的 load_default(size=) 返回**可缩放的** Aileron TrueType；
    #   老版本只能给 10px 位图字体（画出来极小，但至少不崩）。
    try:
        return ImageFont.load_default(size=int(size))
    except Exception:
        try:
            return ImageFont.load_default()
        except Exception:
            return None


def _wrap_text(draw, text, font, max_width, max_lines=2):
    """按可用宽度折行（中文逐字断，西文尽量不切断单词）。"""
    if not text:
        return []
    lines = []
    current = ""
    for ch in str(text):
        candidate = current + ch
        try:
            width = draw.textlength(candidate, font=font)
        except Exception:
            width = len(candidate) * _font_size(font, 12)
        if width <= max_width or not current:
            current = candidate
            continue
        # 西文单词尽量不切断：退回到最后一个空格
        if ch.isalnum() and " " in current:
            cut = current.rfind(" ")
            if cut > 0:
                lines.append(current[:cut].rstrip())
                current = current[cut + 1:] + ch
                continue
        lines.append(current)
        current = ch
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines[:max_lines]


class Menu(BasePlugin):
    """菜单价目表。"""

    def __init__(self, config, **dependencies):
        super().__init__(config, **dependencies)
        # 介绍字体的实际文件名：正常是 DESC_FONT_FILE，
        # 缺失时降级成菜名字体（见 generate_image 里的字体自检）
        self._desc_font_file = DESC_FONT_FILE

    def generate_settings_template(self):
        # ★ 关掉框架内置的 Style 区块（Frame / Margins / Background / Text Color）。
        #   这四组控件对本插件**全都无效**，留着只会误导：
        #     · Frame / Margins：版面是插件自己算的，不吃框架边距；
        #     · Text Color：配色铁律写死（黄只上黑底，白底只有黑和红）；
        #     · Background：框架只有「纯色 / 上传图」两个 radio，
        #       装不下「纯色 / 渐变 / 上传图 / 网络图」四选一 + 连续浓度滑块。
        #   框架页面 JS 里那段回填被 `if (!styleSettings) return;` 包着，
        #   关掉不会抛错（duo_calendar / wifi_setup 早就这么用）。
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    # ------------------------------------------------------------------ 入口
    def generate_image(self, settings, device_config):
        settings = settings if isinstance(settings, dict) else {}
        try:
            width, height = device_config.get_resolution()
        except Exception:
            width, height = 1360, 480
        width, height = int(width), int(height)

        # ★ 字体自检（第三方发布用）：菜名字体缺失就没有中文可画，
        #   直接画说明图；只有介绍字体缺失时用菜名字体顶上，菜单照常显示。
        if not _font_file_ok(NAME_FONT_FILE):
            logger.warning("菜单插件缺少中文字体 %s，改画缺字体说明图", NAME_FONT_FILE)
            return self._render_font_missing((width, height))
        if _font_file_ok(DESC_FONT_FILE):
            self._desc_font_file = DESC_FONT_FILE
        else:
            logger.warning("菜单插件缺少字体 %s，降级用 %s 画介绍",
                           DESC_FONT_FILE, NAME_FONT_FILE)
            self._desc_font_file = NAME_FONT_FILE

        image = self._build_background(settings, (width, height))
        # ★ 背景变了，正文用色也得跟着变（黄底上红字看不见）
        colors = self._ink_colors(image, settings)
        draw = ImageDraw.Draw(image)

        items = self._parse_items(settings)
        if not items:
            self._draw_empty(draw, (width, height))
            return image

        title = str(settings.get("menuTitle") or "").strip()
        layout = str(settings.get("layoutMode") or "columns").strip().lower()
        show_desc = str(settings.get("showDesc") or "1").strip() not in ("0", "false", "no")

        if layout == "grid":
            self._draw_grid(draw, (width, height), items, title, settings, show_desc, colors)
        else:
            self._draw_columns(draw, (width, height), items, title, settings, show_desc, colors)
        return image

    # ------------------------------------------------------------- 缺字体说明图
    def _render_font_missing(self, dimensions):
        """没有可用中文字体时画一张说明图，别让用户对着空白屏猜。

        ★ 文案**必须用英文**：走这条路径的前提就是没有任何可用中文字体，
        写中文会变成一片空白（等于没提示）。
        字体用 Pillow 自带的 Aileron（Pillow >= 10.1 的 load_default(size=)）。
        """
        width, height = dimensions
        image = Image.new("RGB", (width, height), BLACK)
        draw = ImageDraw.Draw(image)

        def ui_font(size):
            try:
                return ImageFont.load_default(size=int(size))
            except Exception:
                try:
                    return ImageFont.load_default()
                except Exception:
                    return None

        title_font = ui_font(max(20, int(height * 0.075)))
        body_font = ui_font(max(13, int(height * 0.038)))
        small_font = ui_font(max(11, int(height * 0.030)))

        margin = int(width * 0.06)
        y = int(height * 0.10)

        # 标题：黑底上用黄色（本插件的配色铁律 —— 黄只上黑底）
        box_h = int(height * 0.14)
        draw.rectangle([margin, y, width - margin, y + box_h], outline=YELLOW, width=4)
        draw.text((width / 2.0, y + box_h / 2.0), "MISSING CHINESE FONT",
                  font=title_font, fill=YELLOW, anchor="mm")

        y += int(height * 0.20)
        lines = [
            "This menu cannot display Chinese text.",
            "",
            "Fix it in either of these ways:",
            "",
            "  1) Copy a Chinese .ttf font into",
            "       src/static/fonts/",
            "",
            "  2) Or point NAME_FONT_FILE / DESC_FONT_FILE",
            "       at a font you already have",
            "       (both are at the top of menu.py)",
        ]
        step = int(height * 0.052)
        for line in lines:
            if line:
                draw.text((margin, y), line, font=body_font, fill=WHITE)
            y += step

        draw.text((margin, height - int(height * 0.115)),
                  "Dish names are blank. See the plugin README for details.",
                  font=small_font, fill=YELLOW)
        return image

    # ------------------------------------------------------------- 背景自适应配色
    @staticmethod
    def _background_ink(background):
        """判断背景主色属于哪一档：deep（深底）/ yellow（黄底）/ light（浅底）。"""
        try:
            small = background.convert("RGB").resize((136, 48))
        except Exception:
            return "light"
        deep = 0
        yellow = 0
        total = 0
        for red, green, blue in small.getdata():
            total += 1
            if max(red, green, blue) < 110:
                deep += 1
            elif red > 170 and green > 170 and blue < 140:
                yellow += 1
        if total == 0:
            return "light"
        if deep / float(total) > 0.55:
            return "deep"
        if yellow / float(total) > 0.40:
            return "yellow"
        return "light"

    @classmethod
    def _ink_colors(cls, background, settings):
        """按背景主色返回正文/价格/分类标签的用色。

        配色铁律（四色屏上每种颜色只有一种合法用法）：
          · 浅底（白/淡彩）-> 黑字、红价（红在白底约 4.7:1）
          · 黄底          -> 全黑字（★ 红在黄底只有约 1.6:1，基本看不见）
          · 深底          -> 白字、黄价（黄在黑底 21:1）
        分类标签也跟着反：浅底/黄底是黑底黄字，深底改成白底黑字。
        """
        mode = cls._background_ink(background)
        if mode == "deep":
            return {"text": WHITE, "price": YELLOW,
                    "tag_bg": WHITE, "tag_text": BLACK}
        if mode == "yellow":
            return {"text": BLACK, "price": BLACK,
                    "tag_bg": BLACK, "tag_text": YELLOW}
        prefer_black = str(settings.get("priceColor") or "red").lower() == "black"
        return {"text": BLACK, "price": BLACK if prefer_black else RED,
                "tag_bg": BLACK, "tag_text": YELLOW}

    # ------------------------------------------------------------------ 背景
    # 背景来源四选一：纯色 / 渐变 / 上传图片 / 网络图片。
    # 统一用一条「背景浓度」滑块（0-100）控制深浅：
    #   0   = 纯白底（最清爽）
    #   100 = 原色 / 原图纹理
    # 四色屏只有黑白黄红，任何中间色都会被网点化 —— 浓度就是网点密度。

    LEGACY_DEPTH = {"1": 12, "2": 28, "3": 55}
    DEFAULT_DEPTH = 0

    def _build_background(self, settings, dimensions):
        """按「来源 + 浓度」生成背景底图。"""
        factor = self._bg_depth(settings) / 100.0
        source = self._bg_source(settings)

        if source == "color":
            base = self._parse_color_exact(settings.get("bgColor"), WHITE)
            return Image.new("RGB", dimensions, self._shade(base, factor))

        if source == "gradient":
            first = self._parse_color_exact(settings.get("bgGradFrom"), WHITE)
            second = self._parse_color_exact(settings.get("bgGradTo"), YELLOW)
            return self._gradient(dimensions, first, second,
                                  str(settings.get("bgGradDir") or "v"), factor)

        path = ""
        if source == "url":
            path = self._fetch_url_image(str(settings.get("bgImageURL") or "").strip())
        else:
            path = str(settings.get("bgImageFile")
                       or settings.get("backgroundImageFile") or "").strip()
        if not path or not os.path.isfile(path):
            # 没有可用图片 -> 退回纯色（老数据里的 backgroundColor 仍然生效）
            base = self._parse_color_exact(
                settings.get("bgColor") or settings.get("backgroundColor"), WHITE)
            return Image.new("RGB", dimensions, self._shade(base, factor))

        try:
            photo = Image.open(path)
            photo = ImageOps.exif_transpose(photo)
            photo.load()
            # 彩色照片在四色屏上必成脏点阵 -> 先转灰度
            gray = photo.convert("L")
            gray = ImageOps.fit(
                gray, dimensions, method=Image.Resampling.LANCZOS, centering=(0.5, 0.35)
            )
            # ★ 按比例压暗（255-(255-v)*factor），不是加常量偏置：
            #   加常量会把浅色照片整体推成纯白，纹理全丢。
            gray = gray.point(
                lambda value: max(0, min(255, 255 - int((255 - value) * factor)))
            )
            dithered = gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
            return dithered.convert("RGB")
        except Exception as exc:
            logger.warning("菜单背景图处理失败，改用纯色: %s", exc)
            return Image.new("RGB", dimensions, self._shade(WHITE, factor))

    # ------------------------------------------------------------- 背景参数
    @classmethod
    def _bg_depth(cls, settings):
        """背景浓度 0-100。老实例只有 bgStrength 三档时按等效刻度换算。"""
        raw = settings.get("bgDepth")
        text = str(raw).strip() if raw is not None else ""
        if not text:
            legacy = str(settings.get("bgStrength") or "").strip()
            if legacy in cls.LEGACY_DEPTH:
                return cls.LEGACY_DEPTH[legacy]
            return cls.DEFAULT_DEPTH
        try:
            return max(0, min(100, int(float(text))))
        except (TypeError, ValueError):
            return cls.DEFAULT_DEPTH

    @staticmethod
    def _bg_source(settings):
        """背景来源。老实例用 backgroundOption（color / image）兜底。"""
        source = str(settings.get("bgSource") or "").strip().lower()
        if source in ("color", "gradient", "image", "url"):
            return source
        option = str(settings.get("backgroundOption") or "").strip().lower()
        return "image" if option == "image" else "color"

    @staticmethod
    def _shade(color, factor):
        """按比例把颜色压向白色（factor=0 纯白，factor=1 原色）。"""
        return tuple(max(0, min(255, int(255 - (255 - channel) * factor)))
                     for channel in color)

    @staticmethod
    def _parse_color_exact(value, default):
        """取**精确** RGB，不吸附到面板四色。

        和 _parse_color 的区别：后者会把任意颜色吸附成黑/白/黄/红，
        那是给框架背景色用的（避免量化噪点）；
        新的纯色/渐变来源要保住用户挑的颜色，网点多少交给浓度滑块管。
        """
        if not value:
            return default
        text = str(value).strip()
        named = {"black": BLACK, "white": WHITE, "yellow": YELLOW, "red": RED}
        if text.lower() in named:
            return named[text.lower()]
        if text.startswith("#"):
            try:
                return ImageColor.getrgb(text)
            except Exception:
                return default
        return default

    @staticmethod
    def _gradient(dimensions, first, second, direction, factor):
        """线性渐变：先把两端按浓度压向白色，再合成。"""
        width, height = dimensions
        first = Menu._shade(first, factor)
        second = Menu._shade(second, factor)
        direction = (direction or "v").strip().lower()

        if direction == "h":
            # 横向：自己铺一条 256 像素的渐变条再放大（别用 rotate，会转成竖的）
            mask = Image.new("L", (256, 1))
            row = mask.load()
            for x in range(256):
                row[x, 0] = x
        elif direction == "d":
            # 斜向：先在 64x64 上画出来再放大（省得逐像素算）
            mask = Image.new("L", (64, 64))
            pixels = mask.load()
            for y in range(64):
                for x in range(64):
                    pixels[x, y] = int(255 * (x + y) / 126.0)
        else:
            mask = Image.linear_gradient("L")

        mask = mask.resize(dimensions, Image.Resampling.BILINEAR)
        return Image.composite(Image.new("RGB", dimensions, second),
                               Image.new("RGB", dimensions, first), mask)

    def _fetch_url_image(self, url):
        """下载网络背景图并缓存到 saved/ 目录。

        要缓存的原因：墨水屏每小时刷一次，不缓存就是每小时重新下一次，
        网络一抖就白刷一次。下载失败一律退回纯色，不让整个插件挂掉。
        """
        if not url:
            return ""
        try:
            import requests
        except Exception as exc:
            logger.warning("菜单背景图下载依赖缺失: %s", exc)
            return ""
        cache_dir = resolve_path(os.path.join("static", "images", "saved"))
        cache_path = os.path.join(
            cache_dir, "url_%s.jpg" % hashlib.md5(url.encode("utf-8")).hexdigest()[:12])
        if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 1024:
            return cache_path
        temp_path = cache_path + ".part"
        try:
            os.makedirs(cache_dir, exist_ok=True)
            response = requests.get(url, timeout=20, stream=True)
            response.raise_for_status()
            total = 0
            with open(temp_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=16384):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > 12 * 1024 * 1024:
                        raise ValueError("图片超过 12MB")
                    handle.write(chunk)
            os.replace(temp_path, cache_path)
            return cache_path
        except Exception as exc:
            logger.warning("菜单背景图下载失败: %s", exc)
            try:
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass
            return ""

    # ------------------------------------------------------------------ 数据
    def _parse_items(self, settings):
        """把 4 组平行数组还原成条目列表。"""
        names = settings.get("item-name[]") or []
        prices = settings.get("item-price[]") or []
        descs = settings.get("item-desc[]") or []
        cats = settings.get("item-cat[]") or []

        def at(seq, index):
            try:
                return seq[index]
            except (IndexError, TypeError):
                return ""

        items = []
        for index, raw_name in enumerate(names):
            name = str(raw_name or "").strip()
            if not name:
                continue
            items.append({
                "name": name,
                "price": str(at(prices, index) or "").strip(),
                "desc": str(at(descs, index) or "").strip(),
                "cat": str(at(cats, index) or "").strip() or "菜单",
            })
        return items

    @staticmethod
    def _group_by_category(items):
        """按分类分组，顺序 = 分类第一次出现的顺序。"""
        order = []
        groups = {}
        for item in items:
            cat = item["cat"]
            if cat not in groups:
                groups[cat] = []
                order.append(cat)
            groups[cat].append(item)
        return [(cat, groups[cat]) for cat in order]

    # ------------------------------------------------------------------ 版式
    def _draw_columns(self, draw, dimensions, items, title, settings, show_desc, colors):
        """分栏版式：一栏一个分类，顶天立地，字号按内容自适应。"""
        width, height = dimensions
        groups = self._group_by_category(items)

        max_cols = self._max_columns(settings)
        cols = max(1, min(len(groups), max_cols))
        col_w = width / float(cols)

        top = 0
        if title:
            top = self._draw_title_bar(draw, width, title)

        content_h = height - top
        head_h = max(28, int(content_h * 0.10))
        avail_h = content_h - head_h - 6

        pad = 16
        inner_w = col_w - 2 * pad

        # 第一遍：用基准字号量一遍，算出能放大的倍数（取最挤的那一栏）
        base_name = max(18, int(content_h * 0.055))
        base_desc = max(12, int(content_h * 0.034))
        name_font = _load_font(NAME_FONT_FILE, base_name)
        desc_font = _load_font(self._desc_font_file, base_desc)

        scale = MAX_SCALE
        for _, cat_items in groups[:cols]:
            natural = self._measure_column(draw, cat_items, name_font, desc_font,
                                           inner_w, show_desc)
            if natural > 0:
                scale = min(scale, avail_h / float(natural))
        scale = max(1.0, scale)

        name_font = _load_font(NAME_FONT_FILE, max(14, int(base_name * scale)))
        desc_font = _load_font(self._desc_font_file, max(11, int(base_desc * scale)))
        price_font = name_font
        head_font = _load_font(NAME_FONT_FILE, max(16, int(head_h * 0.60)))

        for index, (cat, cat_items) in enumerate(groups[:cols]):
            x0 = index * col_w
            x1 = x0 + col_w
            # 分类条：跟着背景反色（黄色只允许出现在黑底上）
            draw.rectangle([x0, top, x1 - 1, top + head_h], fill=colors["tag_bg"])
            self._text(draw, ((x0 + x1) / 2.0, top + head_h / 2.0), cat,
                       head_font, colors["tag_text"], anchor="mm")

            # 栏内容按高度「两端对齐」：条目之间多出来的空隙平摊掉
            heights = [self._measure_item(draw, item, name_font, desc_font,
                                          inner_w, show_desc)
                       for item in cat_items]
            room = avail_h - sum(heights)
            extra = 0
            visible = len(cat_items)
            if visible > 1 and room > 0:
                extra = min(room / float(visible - 1), _font_size(name_font, 20) * 1.6)
            elif room > 0:
                extra = min(room, 20)

            y = top + head_h + 6
            bottom = height
            hidden = 0
            for position, item in enumerate(cat_items):
                block_h = heights[position]
                if y + block_h > bottom:
                    hidden = len(cat_items) - position
                    break
                self._draw_item(draw, item, x0 + pad, y, x1 - pad, name_font,
                                price_font, desc_font, colors, show_desc)
                y += block_h + extra
            if hidden:
                more_font = _load_font(self._desc_font_file, max(11, _font_size(desc_font, 13)))
                self._text(draw, (x0 + pad, height - 4), "还有 %d 项…" % hidden,
                           more_font, colors["price"], anchor="ls")

            # 栏间分隔线
            if index < cols - 1:
                draw.line([x1 - 1, top, x1 - 1, height], fill=BLACK, width=1)

    def _draw_grid(self, draw, dimensions, items, title, settings, show_desc, colors):
        """网格版式：菜品平铺成卡片，字号按格子高度自适应。"""
        width, height = dimensions
        top = 0
        if title:
            top = self._draw_title_bar(draw, width, title)

        show_cat = str(settings.get("showCategory") or "0").strip() not in ("0", "false", "no")

        # 按分类归拢（分类保持首次出现顺序），免得同一个分类被拆到两行
        ordered = []
        for _, cat_items in self._group_by_category(items):
            ordered.extend(cat_items)
        if not ordered:
            ordered = list(items)

        cols = max(1, min(self._max_columns(settings, default=4), len(ordered)))
        rows = max(1, (len(ordered) + cols - 1) // cols)
        content_h = height - top
        cell_w = width / float(cols)
        cell_h = content_h / float(rows)

        pad = 14
        inner_w = cell_w - 2 * pad

        # 第一遍量高度，再算能放大的倍数
        base_name = max(16, int(cell_h * NAME_BASE_RATIO))
        base_desc = max(11, int(cell_h * DESC_BASE_RATIO))
        name_font = _load_font(NAME_FONT_FILE, base_name)
        desc_font = _load_font(self._desc_font_file, base_desc)
        tag_font = (_load_font(NAME_FONT_FILE, max(11, int(base_name * TAG_RATIO)))
                    if show_cat else None)

        scale = min(MAX_SCALE, 1.8)
        natural = 0
        for item in ordered:
            measured = self._measure_item(draw, item, name_font, desc_font,
                                          inner_w, show_desc, tag_font,
                                          max_desc_lines=3)
            natural = max(natural, measured)
        if natural > 0:
            scale = min(scale, (cell_h - 2 * pad) / float(natural))
        scale = max(1.0, scale)

        # 介绍文字单独限一档：跟着菜名一起放大，三行就撑爆格子了
        desc_scale = max(1.0, min(scale, DESC_SCALE_CAP))
        name_font = _load_font(NAME_FONT_FILE, max(13, int(base_name * scale)))
        desc_font = _load_font(self._desc_font_file, max(10, int(base_desc * desc_scale)))
        price_font = name_font
        if tag_font is not None:
            tag_font = _load_font(NAME_FONT_FILE,
                                  max(10, int(base_name * scale * TAG_RATIO)))
        for index, item in enumerate(ordered):
            row = index // cols
            col = index % cols
            x0 = col * cell_w
            y0 = top + row * cell_h
            # 内容在格子里垂直居中，别都顶在上边
            block_h = self._measure_item(draw, item, name_font, desc_font,
                                         inner_w, show_desc, tag_font,
                                         max_desc_lines=3)
            offset = max(0, (cell_h - block_h) / 2.0)
            self._draw_item(draw, item, x0 + pad, y0 + offset,
                            x0 + cell_w - pad, name_font, price_font, desc_font,
                            colors, show_desc, max_desc_lines=3,
                            tag_font=tag_font)
            if col < cols - 1:
                draw.line([x0 + cell_w - 1, y0, x0 + cell_w - 1, y0 + cell_h], fill=BLACK, width=1)

    # ------------------------------------------------------------------ 零件
    def _draw_title_bar(self, draw, width, title):
        """顶部标题条：黑底黄字，下面压一条红线把标题和分类条分开。"""
        height = 54
        draw.rectangle([0, 0, width, height], fill=BLACK)
        font = _load_font(NAME_FONT_FILE, 30)
        self._text(draw, (width / 2.0, height / 2.0), title, font, YELLOW, anchor="mm")
        draw.rectangle([0, height, width, height + 4], fill=RED)
        return height + 4

    def _measure_column(self, draw, cat_items, name_font, desc_font, avail_w, show_desc):
        """一栏内容在当前字号下占多高。"""
        total = 0
        for item in cat_items:
            total += self._measure_item(draw, item, name_font, desc_font, avail_w, show_desc)
        return total

    def _measure_item(self, draw, item, name_font, desc_font, avail_w, show_desc,
                      tag_font=None, max_desc_lines=2):
        """算一个条目块占多高（分类标签 + 名称行 + 介绍行）。

        ★ max_desc_lines 必须和实际绘制时用的一致，
          否则量出来的高度偏小 → 放大倍数算大了 → 内容溢出格子被裁掉。
        """
        height = 0
        if tag_font is not None and item.get("cat"):
            # 标签块 = 文字高 + 上下留白 + 与菜名的间距
            height += _font_size(tag_font, 12) + 8 + 4
        height += _font_size(name_font, 20) + 6
        if show_desc and item.get("desc"):
            lines = _wrap_text(draw, item["desc"], desc_font, avail_w, max_desc_lines)
            height += len(lines) * (_font_size(desc_font, 13) + 4)
        return height

    def _draw_item(self, draw, item, x_left, y_top, x_right, name_font, price_font,
                   desc_font, colors, show_desc, max_desc_lines=2, tag_font=None):
        """画一个条目：分类标签（可选）-> 名称在左、价格在右（同一基线）-> 介绍。"""
        price = item.get("price") or ""
        if price.isdigit():
            price = YUAN_SIGN + price
        name_size = _font_size(name_font, 20)
        desc_size = _font_size(desc_font, 13)

        # 分类小标签：跟着背景反色（黄色只允许出现在黑底上）
        name_top = y_top
        cat = str(item.get("cat") or "").strip()
        if tag_font is not None and cat:
            tag_size = _font_size(tag_font, 12)
            try:
                cat_w = draw.textlength(cat, font=tag_font)
            except Exception:
                cat_w = len(cat) * tag_size
            pad_x = 7
            rect_h = tag_size + 8
            draw.rectangle([x_left, y_top,
                            x_left + cat_w + 2 * pad_x, y_top + rect_h], fill=colors["tag_bg"])
            self._text(draw, (x_left + pad_x, y_top + rect_h / 2.0), cat,
                       tag_font, colors["tag_text"], anchor="lm")
            name_top = y_top + rect_h + 4

        baseline = name_top + name_size

        if price:
            try:
                price_w = draw.textlength(price, font=price_font)
            except Exception:
                price_w = len(price) * 12
            self._text(draw, (x_right, baseline), price, price_font, colors["price"], anchor="rs")
            name_right = x_right - price_w - 12
        else:
            name_right = x_right

        # 菜名太长就压缩字号，保证和价格不撞在一起
        font = name_font
        try:
            name_w = draw.textlength(item["name"], font=font)
        except Exception:
            name_w = len(item["name"]) * 14
        avail = max(40, name_right - x_left)
        if name_w > avail and font is not None:
            smaller = max(12, int(_font_size(font, 14) * avail / max(1, name_w)))
            font = _load_font(NAME_FONT_FILE, smaller)
        self._text(draw, (x_left, baseline), item["name"], font, colors["text"], anchor="ls")

        if show_desc and item.get("desc"):
            lines = _wrap_text(draw, item["desc"], desc_font,
                               max(40, x_right - x_left), max_desc_lines)
            y = baseline + 6
            line_h = desc_size + 4
            for line in lines:
                self._text(draw, (x_left, y), line, desc_font, colors["text"], anchor="lt")
                y += line_h

    def _draw_empty(self, draw, dimensions):
        """一条都没填时给个提示，别让屏上白茫茫一片。"""
        width, height = dimensions
        font = _load_font(NAME_FONT_FILE, 30)
        self._text(draw, (width / 2.0, height / 2.0 - 18), "菜单",
                   font, BLACK, anchor="mm")
        small = _load_font(self._desc_font_file, 18)
        self._text(draw, (width / 2.0, height / 2.0 + 22),
                   "还没有菜品，请在设置页填写名称 / 价格 / 介绍",
                   small, BLACK, anchor="mm")

    # ------------------------------------------------------------------ 小工具
    @staticmethod
    def _text(draw, xy, text, font, fill, anchor="mm"):
        if not text:
            return
        try:
            draw.text(xy, text, font=font, fill=fill, anchor=anchor)
        except Exception:
            try:
                draw.text(xy[:2], text, fill=fill)
            except Exception:
                pass

    @staticmethod
    def _parse_color(value, default):
        """背景色只认面板四色，其余一律映射到最接近的面板色（避免量化时抖成噪点）。"""
        if not value:
            return default
        value = str(value).strip()
        named = {
            "black": BLACK, "white": WHITE, "yellow": YELLOW, "red": RED,
            "#000000": BLACK, "#ffffff": WHITE, "#ffff00": YELLOW, "#ff0000": RED,
        }
        if value.lower() in named:
            return named[value.lower()]
        if value.startswith("#"):
            try:
                rgb = ImageColor.getrgb(value)
            except Exception:
                return default
            return min((BLACK, WHITE, YELLOW, RED),
                       key=lambda c: sum((a - b) ** 2 for a, b in zip(rgb, c)))
        return default

    @staticmethod
    def _max_columns(settings, default=5):
        text = str(settings.get("menuColumns") or "").strip().lower()
        if text in ("auto", ""):
            return default
        try:
            value = int(text)
        except ValueError:
            return default
        return max(1, min(8, value))
