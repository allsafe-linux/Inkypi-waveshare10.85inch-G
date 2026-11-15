# -*- coding: utf-8 -*-
"""WiFi 配网二维码插件：把「热点 / 配网页面」的二维码画到墨水屏上。

配合开机服务 ``wifi-setup.service``（/usr/local/inkypi/bin/wifi_setup_daemon.py）使用。

数据流
------
- daemon 决定要显示什么 → 写 ``/run/inkypi/wifi_setup_ready.json``
  （含 mode=hotspot|url、ssid、password、url、timeout）
- 本插件渲染时读取该文件 → 画二维码 → **回写 displayed_at**（daemon 据此开启配网窗口）
- daemon 的状态变化写 ``/run/inkypi/wifi_setup_state.json``
  （idle / waiting / success / failed / timeout）→ 本插件据此换成结果画面

墨水屏约束：只有黑白红黄四色，白底上只有黑字和红字可读（黄色对比度仅 1.07:1）。
"""

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin.base_plugin import BasePlugin
from utils.app_utils import get_font, resolve_path

LOG_DIR = "/run/inkypi"
READY_FILE = os.path.join(LOG_DIR, "wifi_setup_ready.json")
STATE_FILE = os.path.join(LOG_DIR, "wifi_setup_state.json")
CONF_FILE = "/etc/inkypi/wifi_setup.conf"

# 四色面板调色板
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
RED = (255, 0, 0)
YELLOW = (255, 255, 0)

# 中文字体优先级：粗黑体在墨水屏上笔画最实
CJK_FONT_FILES = ("方正大黑_GBK.ttf", "simhei.ttf", "黑体.ttf", "simkai.ttf", "simfang.ttf")

DEFAULTS = {
    # 仓库里不写死任何可被公开猜到的热点凭据：
    #   真正生效的值来自 /etc/inkypi/wifi_setup.conf（安装脚本随机生成），
    #   或网页 /plugin/wifi_setup 里保存的实例参数。
    "hotspot_ssid": "InkyPi-Setup",
    "hotspot_password": "",
    "setup_port": "8765",
    "window_seconds": "30",
}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read_conf():
    conf = dict(DEFAULTS)
    try:
        with open(CONF_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                conf[key.strip()] = value.strip()
    except OSError:
        pass
    return conf


class WifiSetup(BasePlugin):
    """在墨水屏上显示 WiFi 配网二维码。"""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        template_params["default_hotspot_ssid"] = DEFAULTS["hotspot_ssid"]
        template_params["default_window"] = DEFAULTS["window_seconds"]
        return template_params

    # ------------------------------------------------------------------
    def generate_image(self, settings, device_config):
        dimensions = device_config.get_resolution()
        width, height = dimensions

        ready = _read_json(READY_FILE) or {}
        state = _read_json(STATE_FILE) or {}
        conf = _read_conf()

        mode = ready.get("mode") or "hotspot"
        ssid = ready.get("ssid") or conf["hotspot_ssid"]
        password = ready.get("password") or conf["hotspot_password"]
        url = ready.get("url") or ("http://%s:%s/" % (ready.get("lan_ip", "10.42.0.1"), conf["setup_port"]))
        window_seconds = int(ready.get("timeout") or conf["window_seconds"])

        # 没有密码就没有能连上的热点 —— 别画一个连不上的二维码，
        # 直接退回 URL 模式（守护进程也不会开没有密码的热点）。
        if mode == "hotspot" and not password:
            mode = "url"

        image = Image.new("RGB", dimensions, WHITE)
        draw = ImageDraw.Draw(image)

        status = state.get("state", "")
        if status in ("success", "failed", "timeout"):
            self._draw_result(draw, dimensions, status, state, conf)
        else:
            self._draw_qr_screen(image, draw, dimensions, mode, ssid, password, url, window_seconds)

        # 回写「已上屏」，daemon 据此开启配网窗口
        if ready:
            ready["displayed_at"] = datetime.now().timestamp()
            try:
                with open(READY_FILE, "w", encoding="utf-8") as fh:
                    json.dump(ready, fh, ensure_ascii=False, indent=1)
            except OSError:
                pass

        return image

    # ------------------------------------------------------------------
    def _draw_qr_screen(self, image, draw, dimensions, mode, ssid, password, url, window_seconds):
        width, height = dimensions
        title_font = self._cjk_font(46)
        label_font = self._cjk_font(26)
        value_font = self._cjk_font(34)
        body_font = self._cjk_font(27)
        small_font = self._cjk_font(22)

        pad = 34
        # 顶部标题 + 分隔线
        draw.text((pad, 26), "Pi Zero 无线网络配置", font=title_font, fill=BLACK)
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M")
        draw.text((width - pad, 40), now_text, font=small_font, fill=BLACK, anchor="rs")
        draw.line([(pad, 92), (width - pad, 92)], fill=BLACK, width=3)

        # 左侧二维码
        qr_size = min(height - 150, 430)
        qr_x = pad + 6
        qr_y = 122
        payload = self._qr_payload(mode, ssid, password, url)
        qr_img = self._make_qr(payload, qr_size)
        if qr_img is not None:
            image.paste(qr_img, (qr_x, qr_y))
        else:
            draw.rectangle([qr_x, qr_y, qr_x + qr_size, qr_y + qr_size], outline=BLACK, width=3)
            draw.text((qr_x + qr_size / 2, qr_y + qr_size / 2), "二维码不可用",
                      font=body_font, fill=RED, anchor="mm")

        # 右侧说明
        text_x = qr_x + qr_size + 48
        y = 118

        if mode == "hotspot":
            rows = [("热点名称", ssid), ("热点密码", password)]
        else:
            rows = [("配网页面", url)]
        for label, value in rows:
            draw.text((text_x, y), label, font=label_font, fill=BLACK)
            y += 34
            draw.text((text_x, y - 6), value, font=value_font, fill=RED)
            y += 50

        y += 2
        draw.line([(text_x, y), (width - pad, y)], fill=BLACK, width=2)
        y += 20

        if mode == "hotspot":
            steps = [
                "① 手机相机扫描左侧二维码",
                "② 连接热点 %s" % ssid,
                "③ 选新的 WiFi 并填密码，提交",
            ]
        else:
            steps = [
                "① 手机连上本机所在的 WiFi",
                "② 用手机浏览器打开上面的地址",
                "③ 选新的 WiFi 并填密码，提交",
            ]
        for line in steps:
            draw.text((text_x, y), line, font=body_font, fill=BLACK)
            y += 42

        draw.text((text_x, y + 2), "%d 秒内不操作则沿用原有网络" % window_seconds,
                  font=small_font, fill=BLACK)

    # ------------------------------------------------------------------
    def _draw_result(self, draw, dimensions, status, state, conf):
        width, height = dimensions
        title_font = self._cjk_font(52)
        body_font = self._cjk_font(30)
        small_font = self._cjk_font(24)

        pad = 40
        if status == "success":
            headline = "网络已切换"
            color = RED
            detail = state.get("message", "") or ("已连接到 %s" % state.get("ssid", ""))
        elif status == "failed":
            headline = "连接失败"
            color = RED
            detail = state.get("message", "")
        else:
            headline = "配网窗口已结束"
            color = BLACK
            detail = state.get("message", "") or "设备保持原有网络设置"

        draw.text((pad, 90), headline, font=title_font, fill=color)
        draw.text((pad, 190), detail, font=body_font, fill=BLACK)
        draw.text((pad, 250), "InkyPi 继续正常运行，屏幕会按播放列表轮播。",
                  font=small_font, fill=BLACK)
        draw.text((width - pad, 46), datetime.now().strftime("%Y-%m-%d %H:%M"),
                  font=small_font, fill=BLACK, anchor="rs")

    # ------------------------------------------------------------------
    @staticmethod
    def _qr_payload(mode, ssid, password, url):
        if mode == "hotspot":
            # WIFI:T 格式：手机相机 / 微信扫一扫可直接连上热点
            return "WIFI:T:WPA;S:%s;P:%s;H:false;;" % (ssid, password)
        return url

    @staticmethod
    def _make_qr(payload, target_px):
        """生成二维码位图（纯黑白，避免四色量化时产生噪点）。"""
        if not shutil.which("qrencode"):
            return None
        tmpdir = tempfile.mkdtemp(prefix="inkypi_qr_")
        try:
            probe = os.path.join(tmpdir, "probe.png")
            rc = subprocess.run(
                ["qrencode", "-o", probe, "-t", "PNG", "-s", "1", "-m", "0", "-l", "M", payload],
                capture_output=True, timeout=30,
            )
            if rc.returncode != 0:
                return None
            with Image.open(probe) as probe_img:
                modules = probe_img.size[0]
            margin = 2
            cell = max(1, target_px // (modules + margin * 2))
            side = cell * (modules + margin * 2)
            out = os.path.join(tmpdir, "qr.png")
            rc = subprocess.run(
                ["qrencode", "-o", out, "-t", "PNG", "-s", str(cell), "-m", str(margin),
                 "-l", "M", payload],
                capture_output=True, timeout=30,
            )
            if rc.returncode != 0:
                return None
            with Image.open(out) as qr:
                qr = qr.convert("1")
                if qr.size[0] < target_px:
                    # 最近邻放大到目标边长：模块变大、边缘依然锐利，扫码更稳
                    qr = qr.resize((target_px, target_px), Image.Resampling.NEAREST)
                return qr.convert("RGB")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        finally:
            try:
                for name in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, name))
                os.rmdir(tmpdir)
            except OSError:
                pass

    @staticmethod
    def _cjk_font(font_size):
        for name in CJK_FONT_FILES:
            try:
                return ImageFont.truetype(
                    resolve_path(os.path.join("static", "fonts", name)), font_size
                )
            except OSError:
                continue
        return get_font("Jost", font_size)
