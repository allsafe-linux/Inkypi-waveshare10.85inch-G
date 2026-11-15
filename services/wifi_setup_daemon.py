#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pi Zero 开机 / 重启时的 WiFi 配网服务（配合 InkyPi 的 wifi_setup 插件）。

工作流程
--------
1. 常驻运行（systemd: wifi-setup.service）。
2. 开机后先等 NetworkManager 自己连网（默认 30 秒）；连上了就走「URL 模式」，
   连不上就断开 STA、开热点 ``InkyPi-Setup``（密码取配置 / 实例值）走「热点模式」。
3. 请求 InkyPi 把配网二维码刷到墨水屏（POST /update_now plugin_id=wifi_setup）。
   插件渲染完成后会回写 ``displayed_at``，本服务据此判定「二维码已上屏」，
   **从这一刻起开启 30 秒配网窗口**。
4. 窗口内手机连上热点、打开配网页面提交新的 SSID/密码 → nmcli 切换网络 → 结束。
5. 窗口超时无操作 → 关掉热点、恢复原来的连接 → 正常启动 InkyPi。
6. 配网结束后等 ``restore_display_seconds``（默认 200 秒）把墨水屏刷回正常内容 ——
   墨水屏两次刷新至少间隔 180 秒，不能二维码刚刷完就立刻再刷一次。

为什么热点只在「本机没有局域网 IP」时开
----------------------------------------
Pi Zero 2 W 用的是 brcmfmac43430。实测（2026-11-15）：一旦用 NetworkManager
建立热点，wlan0 的局域网地址会被热点地址顶掉，路由表只剩热点那一段，
局域网彻底不可达。所以「有网时开热点」是有害的，只有在断网救急时才开。

配置：/etc/inkypi/wifi_setup.conf
    hotspot_ssid=InkyPi-Setup
    hotspot_password=<install_steps.sh生成的随机密码>
    setup_port=8765
    window_seconds=30
    lan_wait_seconds=30
    retry_seconds=300
"""

import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("wifi-setup")

RUN_DIR = "/run/inkypi"
CONF_PATH = "/etc/inkypi/wifi_setup.conf"
DEVICE_JSON = "/home/pi/InkyPi-main/src/config/device.json"
READY_FILE = os.path.join(RUN_DIR, "wifi_setup_ready.json")
STATE_FILE = os.path.join(RUN_DIR, "wifi_setup_state.json")
SUBMIT_FILE = os.path.join(RUN_DIR, "wifi_setup_submit.json")
CAPTIVE_CONF_DIR = "/etc/NetworkManager/dnsmasq-shared.d"
CAPTIVE_CONF = os.path.join(CAPTIVE_CONF_DIR, "inkypi-captive.conf")
INKYPI_BASE = "http://127.0.0.1"

DEFAULTS = {
    # ⚠️ 这里**不给**默认密码：仓库里写死一个凭据 = 公开一个凭据。
    #    install_steps.sh 会在安装时随机生成密码写进 /etc/inkypi/wifi_setup.conf；
    #    没配密码时 start_hotspot() 会拒绝开热点（宁可不配网，也不开裸热点）。
    "hotspot_ssid": "InkyPi-Setup",
    "hotspot_password": "",
    "setup_port": "8765",
    "window_seconds": "30",
    "lan_wait_seconds": "30",
    "retry_seconds": "300",
    "inkypi_timeout": "240",
    "restore_display_seconds": "200",
}

HOTSPOT_CONN = "inkypi-setup-hotspot"
HOTSPOT_IP = "192.168.99.1"
HOTSPOT_NET = "192.168.99.0/24"
# 热点网段刻意避开 10.42.0.0/24 —— 那是 k3s / flannel 的 pod 网段，
# 用它做热点会在同一二层里和集群 pod 路由打架（实测：路由被劫持，配网页面打不开）。
HOTSPOT_PREFIX = "192.168.99."


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def load_instance_settings():
    """从 device.json 的播放列表里找 wifi_setup 实例的设置。

    用户在 InkyPi 网页里改的参数（并保存进播放列表）优先级最高，
    这样网页设置和实际开出来的热点永远一致，不会出现「界面写 A、实际是 B」。
    """
    data = read_json(DEVICE_JSON) or {}
    try:
        playlists = data.get("playlist_config", {}).get("playlists", []) or []
        for playlist in playlists:
            for inst in playlist.get("plugins", []) or []:
                if inst.get("plugin_id") == "wifi_setup":
                    return inst.get("plugin_settings", {}) or {}
    except (AttributeError, TypeError):
        pass
    return {}


def load_conf():
    conf = dict(DEFAULTS)
    try:
        with open(CONF_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                conf[key.strip()] = value.strip()
    except FileNotFoundError:
        LOG.warning("配置文件不存在，使用默认值: %s", CONF_PATH)
    except OSError as exc:
        LOG.warning("读取配置失败: %s", exc)

    # 网页里保存过的插件实例设置优先
    inst = load_instance_settings()
    if inst.get("hotspotSSID"):
        conf["hotspot_ssid"] = str(inst["hotspotSSID"]).strip()
    if inst.get("hotspotPassword"):
        conf["hotspot_password"] = str(inst["hotspotPassword"])
    if inst.get("windowSeconds"):
        conf["window_seconds"] = str(inst["windowSeconds"]).strip()
    return conf


def first_playlist_instance():
    """取播放列表里的第一个插件实例，用于把墨水屏恢复成正常内容。

    返回 ``{"playlist": 播放列表名, "plugin_id": 插件 id, "instance": 实例名}``，
    ``playlist_name`` / ``plugin_id`` / ``plugin_instance`` —— 键名写错会被接口判成 400。
    """
    data = read_json(DEVICE_JSON) or {}
    try:
        for playlist in data.get("playlist_config", {}).get("playlists", []) or []:
            for inst in playlist.get("plugins", []) or []:
                pid = inst.get("plugin_id")
                if not pid:
                    continue
                return {
                    "playlist_name": playlist.get("name"),
                    "plugin_id": pid,
                    "plugin_instance": inst.get("name"),
                }
    except (AttributeError, TypeError):
        pass
    return None


def run(cmd, timeout=30):
    """执行命令，返回 (rc, stdout+stderr)。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 127, str(exc)


def nmcli(args, timeout=30):
    return run(["nmcli"] + args, timeout=timeout)


def write_json(path, payload):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        LOG.warning("写入 %s 失败: %s", path, exc)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def ensure_run_dir():
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
    except OSError as exc:
        LOG.warning("创建 %s 失败: %s", RUN_DIR, exc)


# --------------------------------------------------------------------------
# 网络状态
# --------------------------------------------------------------------------

def wlan_ipv4():
    """返回 wlan0 的 IPv4 地址（热点自己的地址要排除）。"""
    rc, out = run(["ip", "-4", "-o", "addr", "show", "wlan0"])
    if rc != 0:
        return None
    for match in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", out):
        addr = match.group(1)
        if addr.startswith(HOTSPOT_PREFIX):
            continue
        return addr
    return None


def has_lan_ip():
    return wlan_ipv4() is not None


def active_wifi_ssid():
    rc, out = nmcli(["-t", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if parts and parts[0] == "yes":
            return parts[1] if len(parts) > 1 else ""
    return None


def current_connection():
    rc, out = nmcli(["-t", "-f", "NAME,DEVICE", "connection", "show", "--active"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[1] == "wlan0":
            return parts[0]
    return None


def scan_wifi():
    """扫描周围 WiFi，返回 [{ssid, signal, security, active}]。"""
    rc, out = nmcli(
        ["-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "yes"],
        timeout=40,
    )
    if rc != 0:
        return []
    seen = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        active, ssid, signal, security = parts[0], parts[1], parts[2], parts[3]
        if not ssid or ssid.startswith("WIFI:"):
            continue
        item = {
            "ssid": ssid,
            "signal": int(signal) if signal.isdigit() else 0,
            "security": security or "--",
            "active": active == "yes",
        }
        old = seen.get(ssid)
        if old is None or item["signal"] > old["signal"]:
            seen[ssid] = item
    return sorted(seen.values(), key=lambda x: (not x["active"], -x["signal"]))


# --------------------------------------------------------------------------
# 热点 / captive portal
# --------------------------------------------------------------------------

def save_sta_credentials():
    """记下当前 STA 连接的 SSID / 密码，断开热点后可以原样连回去。"""
    conn = current_connection()
    if not conn:
        return None
    rc, out = nmcli([
        "--show-secrets", "-g",
        "802-11-wireless.ssid,802-11-wireless-security.psk",
        "connection", "show", conn,
    ])
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    ssid = lines[0] if lines else ""
    psk = lines[1] if len(lines) > 1 else ""
    if not ssid:
        return None
    return {"conn": conn, "ssid": ssid, "psk": psk}


def disconnect_sta():
    """断开当前 STA 连接，把无线网卡完全交给热点。"""
    conn = current_connection()
    if conn:
        nmcli(["connection", "down", conn])
    nmcli(["device", "disconnect", "wlan0"])


def restore_sta(creds=None):
    """恢复自动连接。

    ① 首选 `nmcli connection up <名字>`：连接配置里本来就带着密码，
       不依赖 `--show-secrets`（netplan 建的连接取不到 psk 字段）；
    ② 退路：用保存下来的 SSID + 密码直连（**密码为空时不能传 password 参数**，
       否则 nmcli 报 "802-11-wireless-security.key-mgmt: 缺少属性"）；
    ③ 最后：交给 NetworkManager 自动重连。
    """
    if creds and creds.get("conn"):
        rc, out = nmcli(["connection", "up", creds["conn"]], timeout=60)
        if rc == 0:
            LOG.info("已恢复连接: %s", creds["conn"])
            return True
        LOG.warning("恢复连接 %s 失败: %s", creds["conn"], out.strip())

    if creds and creds.get("ssid"):
        args = ["device", "wifi", "connect", creds["ssid"], "ifname", "wlan0"]
        if creds.get("psk"):
            args += ["password", creds["psk"]]
        rc, out = nmcli(args, timeout=60)
        if rc == 0:
            LOG.info("已重连到 %s", creds["ssid"])
            return True
        LOG.warning("重连 %s 失败: %s", creds["ssid"], out.strip())

    nmcli(["device", "wifi", "rescan", "yes"])
    nmcli(["connection", "up", "wifi"])
    return False


def hotspot_exists():
    rc, out = nmcli(["-t", "-f", "NAME", "connection", "show"])
    if rc != 0:
        return False
    return any(line.strip() == HOTSPOT_CONN for line in out.splitlines())


def start_hotspot(ssid, password):
    """开启热点。返回 True/False。"""
    # ⚠️ 没有密码（或不足 8 位）就**不开热点**：
    #    nmcli 建热点时 psk 为空会退化成**开放网络**（谁都能连），
    #    比「密码是公开值」更糟。所以宁可不配网，也不开裸热点。
    if not password or len(password) < 8:
        LOG.error(
            "未配置热点密码（或不足 8 位），拒绝开启热点 —— "
            "请在 /etc/inkypi/wifi_setup.conf 设置 hotspot_password，"
            "或在网页 /plugin/wifi_setup 里改完点「保存到播放列表」"
        )
        return False
    if hotspot_exists():
        nmcli(["connection", "up", HOTSPOT_CONN])
        return True
    disconnect_sta()
    rc, out = nmcli([
        "connection", "add", "type", "wifi", "ifname", "wlan0",
        "con-name", HOTSPOT_CONN, "ssid", ssid, "autoconnect", "no",
        "wifi.mode", "ap", "wifi.band", "bg",
        "ipv4.method", "shared", "ipv4.addresses", HOTSPOT_IP + "/24",
        "ipv6.method", "ignore",
    ])
    if rc != 0:
        LOG.error("创建热点连接失败: %s", out.strip())
        return False
    rc, out = nmcli([
        "connection", "modify", HOTSPOT_CONN,
        "802-11-wireless-security.key-mgmt", "wpa-psk",
        "802-11-wireless-security.psk", password,
    ])
    if rc != 0:
        LOG.error("设置热点密码失败: %s", out.strip())
    rc, out = nmcli(["connection", "up", HOTSPOT_CONN])
    if rc != 0:
        LOG.error("启动热点失败: %s", out.strip())
        return False
    LOG.info("热点已启动: %s (%s)", ssid, HOTSPOT_IP)
    return True


def stop_hotspot():
    if hotspot_exists():
        nmcli(["connection", "down", HOTSPOT_CONN])
        nmcli(["connection", "delete", HOTSPOT_CONN])
    # 只删热点网段的路由，别把默认路由一起冲掉
    run(["ip", "route", "del", HOTSPOT_NET, "dev", "wlan0"])
    # brcmfmac 从 AP 模式退到 STA 需要一点时间，立刻切网会失败
    time.sleep(3)
    LOG.info("热点已关闭")


def set_captive_portal(enabled):
    """开启/关闭 captive portal：DNS 劫持 + 80 端口转到配网页面。"""
    rule = ["iptables", "-t", "nat", "-C", "PREROUTING", "-i", "wlan0",
            "-p", "tcp", "--dport", "80", "-j", "DNAT",
            "--to-destination", "%s:%s" % (HOTSPOT_IP, "8765")]
    if enabled:
        try:
            os.makedirs(CAPTIVE_CONF_DIR, exist_ok=True)
            with open(CAPTIVE_CONF, "w", encoding="utf-8") as fh:
                fh.write("# 由 inkypi wifi-setup 服务生成：让热点客户端一打开网页就进配网页\n")
                fh.write("address=/#/%s\n" % HOTSPOT_IP)
        except OSError as exc:
            LOG.warning("写 captive portal 配置失败: %s", exc)
        run(rule + ["-A"])
        LOG.info("captive portal 已开启")
    else:
        run(rule + ["-D"])
        try:
            if os.path.exists(CAPTIVE_CONF):
                os.remove(CAPTIVE_CONF)
        except OSError as exc:
            LOG.warning("删除 captive portal 配置失败: %s", exc)
        LOG.info("captive portal 已关闭")


# --------------------------------------------------------------------------
# 切网
# --------------------------------------------------------------------------

def connect_wifi(ssid, password, attempts=3):
    """用 nmcli 连接指定 WiFi。返回 (ok, message)。"""
    args = ["device", "wifi", "connect", ssid, "ifname", "wlan0"]
    if password:
        args += ["password", password]
    last = ""
    for attempt in range(attempts):
        rc, out = nmcli(args, timeout=60)
        if rc == 0:
            # 等 DHCP 拿到地址
            for _ in range(30):
                if has_lan_ip():
                    return True, "已连接到 %s（%s）" % (ssid, wlan_ipv4())
                time.sleep(1)
            last = "已连接但未拿到 IP"
        else:
            last = out.strip() or "连接失败"
        LOG.warning("连接 %s 第 %d 次失败: %s", ssid, attempt + 1, last)
        if attempt + 1 < attempts:
            time.sleep(5)
    return False, last


# --------------------------------------------------------------------------
# 配网 HTTP 服务
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pi Zero 网络配置</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#f2f3f5;font:16px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#1a1a1a}
.wrap{max-width:520px;margin:0 auto;padding:18px 16px 40px}
h1{font-size:20px;margin:4px 0 2px}
.sub{color:#666;font-size:13px;margin-bottom:14px}
.card{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
label{display:block;font-size:13px;color:#555;margin:6px 0 4px}
input{width:100%;padding:11px 12px;border:1px solid #d5d8dc;border-radius:8px;font-size:15px;background:#fafbfc}
input:focus{outline:none;border-color:#2f6fed;background:#fff}
button{width:100%;padding:12px;margin-top:12px;border:0;border-radius:8px;background:#2f6fed;color:#fff;font-size:16px;font-weight:600}
button:disabled{background:#a9b8d6}
button.ghost{background:#eceff3;color:#333}
.wifi{list-style:none;margin:0;padding:0}
.wifi li{border-bottom:1px solid #eef0f2}
.wifi li:last-child{border-bottom:0}
.wifi button{width:100%;text-align:left;background:none;color:#1a1a1a;font-weight:400;font-size:15px;padding:11px 2px;border-radius:0;margin:0;display:flex;justify-content:space-between}
.wifi button.sel{color:#2f6fed;font-weight:600}
.tag{font-size:12px;color:#888;font-weight:400}
.bar{height:6px;background:#e8ebef;border-radius:3px;overflow:hidden;margin:8px 0 2px}
.bar i{display:block;height:100%;background:#2f6fed;width:100%;transition:width .3s linear}
#msg{font-size:14px;padding:10px 12px;border-radius:8px;display:none}
#msg.ok{background:#e6f6ec;color:#14682c;display:block}
#msg.err{background:#fdecec;color:#a12020;display:block}
#msg.info{background:#eaf1fd;color:#1b4f9c;display:block}
.small{font-size:12px;color:#888}
</style></head><body>
<div class="wrap">
  <h1>Pi Zero 无线网络配置</h1>
  <div class="sub">扫描并选择要连接的 WiFi</div>

  <div class="card">
    <div id="status" class="small">正在读取状态…</div>
    <div class="bar"><i id="countdown"></i></div>
    <div id="msg"></div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <strong style="font-size:15px">附近的 WiFi</strong>
      <button class="ghost" id="rescan" style="width:auto;padding:7px 12px;margin:0;font-size:13px">重新扫描</button>
    </div>
    <ul class="wifi" id="list"><li><div class="small" style="padding:11px 2px">正在扫描…</div></li></ul>
    <label for="ssid">网络名称（SSID）</label>
    <input id="ssid" placeholder="选择上方列表或手动输入">
    <label for="pass">密码（WPA / WPA2；开放网络留空）</label>
    <input id="pass" type="text" placeholder="留空表示不加密">
    <button id="submit">连接到该网络</button>
  </div>

  <div class="card small">
    本机为 Pi Zero 2 W（10.85 寸四色墨水屏）。提交后设备会自动切换到新网络并继续运行。
    若 30 秒内没有操作，设备会保持原有网络设置。
  </div>
</div>
<script>
var sel = "", timer = null, deadline = 0;
function esc(s){return (s+'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function msg(t,k){var m=document.getElementById('msg');m.textContent=t;m.className=k||'info';}
function tick(){
  var left=Math.max(0,Math.ceil((deadline-Date.now()/1000)));
  document.getElementById('countdown').style.width=(deadline? (left/window.total*100):0)+'%';
  document.getElementById('status').textContent = deadline ? ('配网窗口剩余 '+left+' 秒') : '等待设备开启配网窗口';
}
function scan(){
  var l=document.getElementById('list');l.innerHTML='<li><div class="small" style="padding:11px 2px">正在扫描…</div></li>';
  fetch('/api/scan').then(function(r){return r.json();}).then(function(d){
    if(!d.wifi||!d.wifi.length){l.innerHTML='<li><div class="small" style="padding:11px 2px">没扫到网络，可手动输入名称</div></li>';return;}
    l.innerHTML='';
    d.wifi.forEach(function(w){
      var li=document.createElement('li'), b=document.createElement('button');
      b.innerHTML='<span>'+esc(w.ssid)+(w.active?' <span class="tag">已连接</span>':'')+'</span><span class="tag">'+esc(w.security)+' · '+w.signal+'%</span>';
      b.onclick=function(){sel=w.ssid;document.getElementById('ssid').value=w.ssid;
        [].forEach.call(l.querySelectorAll('button'),function(x){x.classList.remove('sel');});b.classList.add('sel');};
      li.appendChild(b);l.appendChild(li);
    });
  }).catch(function(){l.innerHTML='<li><div class="small" style="padding:11px 2px">扫描失败，点右上角重试</div></li>';});
}
function status(){
  fetch('/api/status').then(function(r){return r.json();}).then(function(d){
    window.total=d.window||30; deadline=d.deadline||0; tick();
    if(d.state==='success'){msg('已切换到 '+(d.ssid||'')+'，本页面可以关闭了','ok');}
    else if(d.state==='failed'){msg('连接失败：'+(d.message||'未知错误'),'err');}
    else if(d.state==='timeout'){msg('配网窗口已结束，设备保持原有网络','info');}
    else if(d.hotspot){msg('请先连接热点 '+(d.ssid||'')+'，密码 '+(d.password||''),'info');}
  }).catch(function(){});
}
document.getElementById('rescan').onclick=scan;
document.getElementById('submit').onclick=function(){
  var ssid=document.getElementById('ssid').value.trim(), pass=document.getElementById('pass').value;
  if(!ssid){msg('请先选择或输入网络名称','err');return;}
  var b=this; b.disabled=true; b.textContent='正在连接…';
  fetch('/api/connect',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ssid:sssid,password:pass})})
    .then(function(r){return r.json();})
    .then(function(d){ if(d.ok){msg('正在切换到 '+ssid+'…','ok'); setTimeout(status,1500);} else {msg('失败：'+(d.message||''),'err'); b.disabled=false; b.textContent='连接到该网络';} })
    .catch(function(){msg('请求失败，请确认还连在热点上','err');b.disabled=false;b.textContent='连接到该网络';});
};
scan(); status(); timer=setInterval(tick,500); setInterval(status,3000);
</script>
</body></html>
"""


class SetupHandler(BaseHTTPRequestHandler):
    server_version = "InkyPiSetup/1.0"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE)
        elif path == "/api/status":
            self._send(200, json.dumps(self.server.app.status_payload(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif path == "/api/scan":
            self._send(200, json.dumps({"wifi": scan_wifi()}, ensure_ascii=False),
                       "application/json; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/api/connect":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, OSError):
            payload = {}
        ssid = str(payload.get("ssid", "")).strip()
        password = str(payload.get("password", ""))
        if not ssid:
            self._send(400, json.dumps({"ok": False, "message": "缺少网络名称"}, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        if not self.server.app.in_window():
            # 安全保护：只有配网窗口开放时才接受改网络的请求
            self._send(403, json.dumps({"ok": False, "message": "不在配网窗口内，已忽略"}, ensure_ascii=False),
                       "application/json; charset=utf-8")
            return
        LOG.info("收到配网请求: %s", ssid)
        self.server.app.submit(ssid, password)
        self._send(200, json.dumps({"ok": True, "message": "已提交"}, ensure_ascii=False),
                   "application/json; charset=utf-8")


class SetupServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app):
        self.app = app
        super().__init__(addr, SetupHandler)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

class WifiSetupApp:
    def __init__(self, conf):
        self.conf = conf
        self.lock = threading.Lock()
        self.state = "idle"
        self.message = ""
        self.target_ssid = ""
        self.hotspot = False
        self.hotspot_ssid = conf["hotspot_ssid"]
        self.hotspot_password = conf["hotspot_password"]
        self.deadline = 0
        self.window_seconds = int(conf["window_seconds"])
        self.pending = None
        self.submit_event = threading.Event()
        self.saved_sta = save_sta_credentials()
        self.next_qr_at = 0
        self.qr_displayed = False

    # ---- 状态 ----
    def status_payload(self):
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "ssid": self.target_ssid,
                "hotspot": self.hotspot,
                "hotspot_ssid": self.hotspot_ssid if self.hotspot else "",
                "password": self.hotspot_password if self.hotspot else "",
                "window": self.window_seconds,
                "deadline": self.deadline,
                "lan_ip": wlan_ipv4() or "",
                "connected_ssid": active_wifi_ssid() or "",
            }

    def submit(self, ssid, password):
        with self.lock:
            self.pending = (ssid, password)
        self.submit_event.set()

    def _set_deadline(self, value):
        with self.lock:
            self.deadline = value
    def in_window(self):
        """当前是否处于配网窗口（只有窗口内才允许改网络）。"""
        with self.lock:
            return bool(self.deadline) and time.time() < self.deadline

    # ---- 触发墨水屏显示二维码 ----
    def wait_for_inkypi(self, timeout=180):
        """等 InkyPi 的 Web 服务起来（开机后约 20 秒）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(INKYPI_BASE + "/", timeout=5) as resp:
                    resp.read(64)
                return True
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(3)
        return False

    def request_qr_display(self, mode, ssid, password, url):
        """写 ready 文件并请求 InkyPi 把二维码刷到墨水屏。

        ``/update_now`` 是**同步**接口：它返回时墨水屏已经刷完，
        所以「POST 返回的那一刻」就是二维码上屏的时刻 —— 配网窗口从这一刻开始算。
        """
        ensure_run_dir()
        payload = {
            "mode": mode,
            "ssid": ssid,
            "password": password,
            "url": url,
            "timeout": self.window_seconds,
            "requested_at": time.time(),
            "displayed_at": 0,
        }
        write_json(READY_FILE, payload)
        self._set_state("waiting")

        body = urllib.parse.urlencode({"plugin_id": "wifi_setup"}).encode()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(INKYPI_BASE + "/update_now", data=body),
                timeout=int(self.conf["inkypi_timeout"]),
            ) as resp:
                raw = resp.read().decode("utf-8", "replace")
            try:
                result = json.loads(raw)
            except ValueError:
                result = {}
            if not result.get("success"):
                # 注意：/update_now 在插件找不到时也会返回 200 + success，
                # 真正的失败信息在 refresh_task 的日志里 → 这里只能按返回体判断
                LOG.warning("InkyPi 刷新返回异常: %s", raw[:200])
                return False
            LOG.info("二维码已刷到墨水屏")
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOG.warning("触发 InkyPi 刷新失败: %s", exc)
            return False

    # ---- 配网结束后把墨水屏恢复成正常内容 ----
    def restore_display(self):
        """在后台线程里，等够时间后把墨水屏刷回正常的播放内容。

        墨水屏硬约束：两次刷新之间至少间隔 180 秒（官方手册），刷太勤会加速残影。
        二维码刚刷完就必须等够时间再刷回去，所以这里默认延迟 200 秒。
        """
        delay = int(self.conf.get("restore_display_seconds", 200))
        info = first_playlist_instance()
        if not info:
            LOG.info("播放列表里没有可恢复的实例，跳过恢复显示")
            return
        thread = threading.Thread(
            target=self._restore_display_worker, args=(delay, info), daemon=True
        )
        thread.start()

    def _restore_display_worker(self, delay, info):
        time.sleep(delay)
        body = json.dumps(info).encode("utf-8")
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    INKYPI_BASE + "/display_plugin_instance",
                    data=body,
                    headers={"Content-Type": "application/json"},
                ),
                timeout=int(self.conf["inkypi_timeout"]),
            ) as resp:
                raw = resp.read().decode("utf-8", "replace")
            LOG.info("已把墨水屏恢复为正常内容: %s", raw[:120])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOG.warning("恢复显示失败: %s", exc)

    # ---- 主循环 ----
    def run(self):
        setup_port = int(self.conf["setup_port"])
        server = SetupServer(("0.0.0.0", setup_port), self)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        LOG.info("配网页面已启动: http://<本机IP>:%d/", setup_port)

        lan_wait = int(self.conf["lan_wait_seconds"])
        retry = int(self.conf["retry_seconds"])

        # 等 InkyPi 的 Web 服务起来（开机后约 20 秒）
        if not self.wait_for_inkypi():
            LOG.warning("InkyPi 没在 %s 秒内就绪，配网页面仍可用", self.conf["inkypi_timeout"])

        # 开机后先等 NetworkManager 自己连网
        LOG.info("等待 %d 秒让 NetworkManager 自动连接…", lan_wait)
        deadline = time.time() + lan_wait
        while time.time() < deadline:
            if has_lan_ip():
                break
            time.sleep(2)

        first_round = True
        while True:
            if has_lan_ip():
                # 有网：不开热点（会顶掉局域网地址），只提供网页配网入口
                if first_round or (self.next_qr_at and time.time() >= self.next_qr_at):
                    LOG.info("已连接到局域网 %s，不开热点（保持局域网可达）", wlan_ipv4())
                    url = "http://%s:%d/" % (wlan_ipv4(), setup_port)
                    self.request_qr_display("url", "", "", url)
                    # 刷屏要阻塞 90 秒，这期间网络可能已经断了 —— 复检一次，
                    # 否则会拿着一个打不开的 URL 干等 30 秒
                    if not has_lan_ip():
                        LOG.warning("刷屏期间网络已断开，转入配网模式")
                        first_round = True
                        continue
                    self._set_deadline(time.time() + self.window_seconds)
                    self.submit_event.clear()
                    ok = self._wait_for_submit()
                    self._set_deadline(0)
                    if ok:
                        ssid, password = self.pending
                        LOG.info("收到配网请求: %s", ssid)
                        good, message = connect_wifi(ssid, password)
                        self.target_ssid = ssid
                        if good:
                            self._set_state("success", message)
                            LOG.info("配网成功: %s", message)
                            self.next_qr_at = 0
                            self.qr_displayed = False
                            self.restore_display()
                        else:
                            self._set_state("failed", message)
                            LOG.error("配网失败: %s", message)
                            self.next_qr_at = time.time() + retry
                    else:
                        LOG.info("有网模式窗口结束，未做改动")
                        self._set_state("idle", "已连接原有网络")
                        self.restore_display()
                    first_round = False
                else:
                    # 平时快速轮询：一掉线立刻进入配网模式（救急场景不能等 5 分钟）
                    time.sleep(5)
                continue

            # 没网：断 STA、开热点，进入配网模式
            LOG.info("没有局域网地址，进入配网模式")
            self.hotspot = True
            self.saved_sta = save_sta_credentials() or self.saved_sta
            start_hotspot(self.hotspot_ssid, self.hotspot_password)
            set_captive_portal(True)
            time.sleep(3)
            url = "http://%s:%d/" % (HOTSPOT_IP, setup_port)
            if not self.qr_displayed:
                # 墨水屏上还没有二维码 → 刷一次（刷屏要阻塞 90 秒）
                self.request_qr_display("hotspot", self.hotspot_ssid, self.hotspot_password, url)
                self.qr_displayed = True

            # 窗口内可以反复提交（密码填错能重试），二维码一直留在屏上不重刷
            while True:
                self._set_deadline(time.time() + self.window_seconds)
                self.submit_event.clear()
                ok = self._wait_for_submit()
                self._set_deadline(0)

                if not ok:
                    LOG.info("配网窗口超时")
                    self._after_setup_timeout()
                    break

                ssid, password = self.pending
                LOG.info("开始切换到 %s", ssid)
                set_captive_portal(False)
                stop_hotspot()
                good, message = connect_wifi(ssid, password)
                self.target_ssid = ssid
                if good:
                    self._set_state("success", message)
                    LOG.info("配网成功: %s", message)
                    self.qr_displayed = False
                    self.hotspot = False
                    self.restore_display()
                    break

                # 切换失败：把热点重新开起来，再开一个窗口让用户在页面上重试
                self._set_state("failed", message)
                LOG.error("配网失败: %s", message)
                start_hotspot(self.hotspot_ssid, self.hotspot_password)
                set_captive_portal(True)
                self.hotspot = True
                time.sleep(3)
                self._set_state("waiting", "连接失败，请重试")

            first_round = False
            time.sleep(retry)

    def _after_setup_timeout(self):
        """配网窗口超时后的收尾。

        ★ 顺序很关键：**必须先把热点关掉，再去恢复原网络**。
        brcmfmac 的无线网卡同一时刻只能是一种模式（AP 或 STA），
        热点还开着的时候执行 ``nmcli connection up <原网络>`` 会一直转圈直到超时，
        结果就是设备卡在「热点模式」里出不来（实测 2026-11-15）。

        优先级（**绝不能把设备留在「热点模式」出不来**）：
        ① 关热点 → 恢复原网络 → 成功就沿用原网络；
        ② 恢复不了（本来就没网）→ 把热点重新开回来，配网页面继续可用，
           用户随时能配；墨水屏上保留着二维码，不重复刷新（刷一次 90 秒，频繁刷伤屏）。
        """
        set_captive_portal(False)
        stop_hotspot()

        restored = False
        if self.saved_sta:
            restore_sta(self.saved_sta)
            for _ in range(25):
                if has_lan_ip():
                    restored = True
                    break
                time.sleep(1)
        if restored:
            LOG.info("已恢复原有网络 %s", wlan_ipv4())
            self._set_state("timeout", "配网窗口已结束，已沿用原有网络")
            self.hotspot = False
            self.qr_displayed = False
            self.restore_display()
            return

        LOG.warning("原网络无法恢复，重新开热点等待用户配网")
        start_hotspot(self.hotspot_ssid, self.hotspot_password)
        set_captive_portal(True)
        self.hotspot = True
        self._set_state("timeout", "配网窗口已结束，热点仍开启，可随时配网")

    def _wait_for_submit(self):
        while time.time() < self.deadline:
            if self.submit_event.wait(timeout=1):
                return True
        return False

    def _set_state(self, state, message=""):
        with self.lock:
            self.state = state
            self.message = message
            payload = {
                "state": state,
                "message": message,
                "ssid": self.target_ssid,
                "ts": time.time(),
            }
        write_json(STATE_FILE, payload)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    ensure_run_dir()
    conf = load_conf()
    app = WifiSetupApp(conf)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        LOG.exception("配网服务异常退出")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
