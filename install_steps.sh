#!/usr/bin/env bash
# =============================================================================
# InkyPi x 微雪 10.85" 四色 e-Paper (G) 一键部署脚本
#
# 目标设备: Pi Zero 2 W / Debian 13 (trixie) arm64
# 源码目录: /home/pi/InkyPi-main   (官方默认路径, 不要改)
#
# 用法:
#   sudo bash install_steps.sh
#
# 幂等: 可重复执行（已存在的文件会先备份为 .bak-<时间戳>）
# =============================================================================
set -euo pipefail

SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${INKYPI_REPO:-/home/pi/InkyPi-main}"
STAMP="$(date +%Y%m%d-%H%M%S)"

say()  { echo -e "\n\033[1;36m==> $*\033[0m"; }
HS_SSID=""; HS_PSK=""

# 从配置文件里取某个键的值（第一处匹配）。纯 bash 实现，
# 避免 sed|head 管道在 set -o pipefail 下被 SIGPIPE 打断。
conf_get() {
    local file="$1" key="$2" line val
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ "$line" =~ ^[[:space:]]*"$key"[[:space:]]*=(.*)$ ]]; then
            val="${BASH_REMATCH[1]}"
            printf '%s\n' "$val"
            return 0
        fi
    done < "$file"
    return 0
}
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()  { echo -e "\033[1;31m[fail]\033[0m $*" >&2; exit 1; }

# -----------------------------------------------------------------------------
say "0/9 检查运行环境"
if [ ! -d "$REPO/src" ]; then
    die "找不到 $REPO/src —— 请先 git clone InkyPi 到 $REPO（目录名必须是 InkyPi-main，官方软链写死了它）"
fi
echo "  源码目录: $REPO"
python3 -c 'import sys; print("  系统 Python:", sys.version.split()[0])'

# -----------------------------------------------------------------------------
say "1/9 修复 chromium 包版本错配（不修会导致渲染永久 hang）"
# 症状: chromium-headless-shell 是 iU 状态且与 chromium-common 版本不一致
#       -> V8 快照与 libchromium.so 不匹配 -> renderer 秒崩 -> 上游 take_screenshot
#          没有 timeout -> 网页请求永久挂死
if dpkg -l | grep -q 'chromium-headless-shell'; then
    echo "  当前状态:"
    dpkg -l | grep -i chromium | awk '{printf "    %-28s %s\n", $2, $3}'
    if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
            chromium chromium-common chromium-headless-shell chromium-l10n >/dev/null 2>&1; then
        warn "apt 安装失败，尝试仅重装已存在的包"
        sudo DEBIAN_FRONTEND=noninteractive apt-get install --reinstall -y \
            chromium chromium-common chromium-headless-shell chromium-l10n || \
            warn "chromium 重装仍失败，请手动检查 dpkg -l | grep chromium"
    fi
    echo "  修复后状态:"
    dpkg -l | grep -i chromium | awk '{printf "    %-28s %s\n", $2, $3}'
else
    warn "未安装 chromium —— 渲染 HTML 插件将不可用"
fi

# -----------------------------------------------------------------------------
say "2/9 备份将要覆盖的文件"
backup() {
    local f="$1"
    if [ -f "$f" ]; then
        cp -p "$f" "${f}.bak-${STAMP}"
        echo "  备份 $f -> ${f}.bak-${STAMP}"
    fi
}

# -----------------------------------------------------------------------------
say "3/9 铺设四色驱动（必须在官方 install.sh 之前）"
# 官方仓库根本没有 10.85\" (G) 的 Python 四色驱动，install.sh -W epd10in85g 会 404
mkdir -p "$REPO/src/display/waveshare_epd"
for f in epd10in85g.py epdconfig.py; do
    src="$SRC_ROOT/src/display/waveshare_epd/$f"
    dst="$REPO/src/display/waveshare_epd/$f"
    [ -f "$src" ] || die "缺少 $src"
    backup "$dst"
    cp "$src" "$dst"
    chmod 666 "$dst"
    echo "  -> $dst"
done

# -----------------------------------------------------------------------------
say "4/9 应用完整 patch（框架层 + 墨水屏间隔 + 渲染加固 + To-Do / Meeting / Duo Calendar / WiFi Setup / Menu 插件）"
PATCH="$SRC_ROOT/inkypi_10in85g.patch"
PATCH_FILES="src/display/waveshare_display.py
src/display/waveshare_epd/epd10in85g.py
src/model.py
src/utils/image_utils.py
src/plugins/base_plugin/base_plugin.py
src/plugins/todo_list/todo_list.py
src/plugins/todo_list/settings.html
src/plugins/todo_list/render/todo_list.html
src/plugins/todo_list/render/todo_list.css
src/plugins/meeting/meeting.py
src/plugins/meeting/settings.html
src/plugins/meeting/plugin-info.json
src/plugins/meeting/icon.png
src/plugins/meeting/render/meeting.html
src/plugins/meeting/render/meeting.css
src/plugins/duo_calendar/duo_calendar.py
src/plugins/duo_calendar/lunar.py
src/plugins/duo_calendar/lunar_data.py
src/plugins/duo_calendar/settings.html
src/plugins/duo_calendar/plugin-info.json
src/plugins/duo_calendar/icon.png
src/plugins/wifi_setup/wifi_setup.py
src/plugins/wifi_setup/settings.html
src/plugins/wifi_setup/plugin-info.json
src/plugins/wifi_setup/icon.png
src/plugins/menu/menu.py
src/plugins/menu/settings.html
src/plugins/menu/plugin-info.json
src/plugins/menu/icon.png"
if [ -f "$PATCH" ]; then
    for f in $PATCH_FILES; do
        backup "$REPO/$f"
    done
    if (cd "$REPO" && git apply --check --binary "$PATCH" 2>/dev/null); then
        (cd "$REPO" && git apply --binary "$PATCH")
        echo "  patch 已干净应用"
    elif (cd "$REPO" && git apply --check --binary --reverse "$PATCH" 2>/dev/null); then
        echo "  patch 已经应用过了，跳过（当前状态已匹配）"
    else
        die "patch 应用失败 —— 请检查 $REPO 是否有额外的手工改动"
    fi
    # 双保险：图标是二进制，万一 --binary 没生效就单独补拷
    for icon in meeting duo_calendar wifi_setup menu; do
        if [ -f "$SRC_ROOT/src/plugins/$icon/icon.png" ]; then
            mkdir -p "$REPO/src/plugins/$icon"
            cp "$SRC_ROOT/src/plugins/$icon/icon.png" "$REPO/src/plugins/$icon/icon.png"
        fi
    done
else
    warn "找不到 patch 文件，跳过（改为逐文件复制）"
    for f in $PATCH_FILES; do
        src="$SRC_ROOT/$f"
        dst="$REPO/$f"
        [ -f "$src" ] || die "缺少 $src"
        mkdir -p "$(dirname "$dst")"
        backup "$dst"
        cp "$src" "$dst"
    done
    echo "  已逐文件复制 $(echo "$PATCH_FILES" | wc -l) 个文件"
fi

# -----------------------------------------------------------------------------
say "5/9 写入设备配置 device.json"
# ★ 和上游 install.sh 一个规矩，但更严格一点：
#   device.json 是**用户自己的配置**（播放列表、实例参数、上传素材的绝对路径），
#   覆盖它等于把人家配置清空。所以：
#     - 文件不存在            -> 写入出厂模板
#     - 还是上游那份没动过的最小模板 -> 换成我们的出厂模板（带 10.85G 的 display_type）
#     - 已经被改过            -> 一律保留，只提示
if [ -f "$REPO/src/config/device.json" ]; then
    if cmp -s "$REPO/src/config/device.json" "$REPO/install/config_base/device.json"; then
        backup "$REPO/src/config/device.json"
        cp "$SRC_ROOT/src/config/device.json" "$REPO/src/config/device.json"
        echo "  -> 已把上游的最小模板换成本项目的出厂模板"
    else
        echo "  已存在 device.json 且被改动过 —— 保留不动（那是你的配置）"
        echo "  想换回出厂模板：先备份，再删掉它，然后重跑本脚本"
    fi
else
    cp "$SRC_ROOT/src/config/device.json" "$REPO/src/config/device.json"
    echo "  -> $REPO/src/config/device.json（出厂模板）"
fi
chown pi:pi "$REPO/src/config/device.json" 2>/dev/null || true
chmod 644 "$REPO/src/config/device.json"
echo "  (display_type=epd10in85g, resolution=[1360,480], 模板自带待办 + 菜单两个示例实例)"

# -----------------------------------------------------------------------------
say "6/9 恢复中文字体（正本在系统 /usr/share/fonts/truetype/windows/）"
FONT_SRC="/usr/share/fonts/truetype/windows"
FONT_DST="$REPO/src/static/fonts"
mkdir -p "$FONT_DST"
if [ -d "$FONT_SRC" ]; then
    n=0
    for f in "$FONT_SRC"/*.ttf "$FONT_SRC"/*.TTF; do
        [ -f "$f" ] || continue
        base="$(basename "$f")"
        # 只补仓库里没有的（仓库自带的 Jost/Napoli/Dogica/DS-DIGI 不动）
        if [ ! -f "$FONT_DST/$base" ]; then
            cp "$f" "$FONT_DST/$base"
            n=$((n+1))
        fi
    done
    chown -R pi:pi "$FONT_DST" 2>/dev/null || true
    echo "  补齐中文字体 $n 个"
    ls -1 "$FONT_DST" | sed 's/^/    /'
else
    warn "$FONT_SRC 不存在 —— 请手动把 9 个中文 TTF 放到 $FONT_DST"
    warn "  方正大黑_GBK / 方正小标宋_GBK / 方正魏碑_GBK / 方正卡通简体 /"
    warn "  楷体_GB2312 / 仿宋_GB2312 / simhei / simkai / simfang"
fi

# -----------------------------------------------------------------------------
say "7/9 补装 venv 依赖（官方 install.sh 的 pip 是后台跑的，容易被打断）"
VENV="/usr/local/inkypi/venv_inkypi"
if [ -x "$VENV/bin/pip" ]; then
    echo "  用 sudo 前台安装（venv 是 root 拥有，普通用户会 Permission denied）"
    sudo "$VENV/bin/pip" install -q -r "$REPO/install/requirements.txt" || \
        warn "pip 安装有报错，请手动检查 $VENV/bin/pip list"
    "$VENV/bin/python" - <<'PY'
mods = ["flask", "waitress", "PIL", "numpy", "spidev", "pi_heif", "inky", "requests"]
missing = []
for m in mods:
    try:
        __import__(m)
    except Exception:
        missing.append(m)
print("  依赖检查:", "全部就位" if not missing else "缺失 -> " + ", ".join(missing))
PY
else
    warn "venv 不存在 ($VENV) —— 请先跑官方 install.sh，再重跑本脚本"
fi

# -----------------------------------------------------------------------------
say "8/9 安装开机二维码配网服务（wifi-setup.service）"
# 依赖：qrencode 生成二维码 / iptables 做 captive portal 的 NAT / dnsmasq 给热点做 DHCP+DNS
sudo apt-get install -y qrencode iptables dnsmasq 2>&1 | tail -2
# ★ dnsmasq 只能装、不能开机自启：系统级 dnsmasq 会抢占 53 端口，
#   导致 NetworkManager 自己的 dnsmasq 起不来 → 热点没有 DHCP（手机连上拿不到 IP）。
#   实测报错：failed to create listening socket for 192.168.99.1: 地址已在使用
sudo systemctl disable --now dnsmasq.service 2>&1 | tail -1
if [ -d "$SRC_ROOT/services" ]; then
    sudo mkdir -p /usr/local/inkypi/bin /etc/inkypi
    sudo cp "$SRC_ROOT/services/wifi_setup_daemon.py" /usr/local/inkypi/bin/wifi_setup_daemon.py
    sudo chmod 755 /usr/local/inkypi/bin/wifi_setup_daemon.py

    # --- 热点凭据：先记下旧值，装完新配置再写回去 -----------------------
    # 仓库里不带任何默认密码，所以这里要么沿用你已有的，要么随机生成一对。
    OLD_SSID="$(conf_get /etc/inkypi/wifi_setup.conf hotspot_ssid)"
    OLD_PSK="$(conf_get /etc/inkypi/wifi_setup.conf hotspot_password)"
    sudo cp "$SRC_ROOT/services/wifi_setup.conf" /etc/inkypi/wifi_setup.conf
    if [ -n "${OLD_PSK:-}" ]; then
        HS_SSID="${OLD_SSID:-InkyPi-Setup}"
        HS_PSK="$OLD_PSK"
        echo "  沿用你已有的热点凭据: $HS_SSID"
    else
        HS_SSID="InkyPi-Setup-$(head -c 3 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-4)"
        HS_PSK="$(head -c 9 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-16)"
        echo "  已生成随机热点凭据: $HS_SSID"
    fi
    sudo sed -i "s|^[[:space:]]*hotspot_ssid[[:space:]]*=.*|hotspot_ssid=$HS_SSID|" /etc/inkypi/wifi_setup.conf
    sudo sed -i "s|^[[:space:]]*hotspot_password[[:space:]]*=.*|hotspot_password=$HS_PSK|" /etc/inkypi/wifi_setup.conf
    sudo chmod 600 /etc/inkypi/wifi_setup.conf

    sudo cp "$SRC_ROOT/services/wifi-setup.service" /etc/systemd/system/wifi-setup.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now wifi-setup.service
    echo "  -> wifi-setup.service 已启用（开机自动配网二维码）"
    echo "  -> 热点密码: $HS_PSK  （记下来；配网二维码里也有）"
else
    warn "找不到 services/ 目录，跳过配网服务安装"
fi

# -----------------------------------------------------------------------------
say "9/9 重启服务并等待就绪"
sudo systemctl restart inkypi || warn "systemctl restart 失败"
printf "  等待端口 80 "
for i in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/ || true)"
    if [ "$code" = "200" ]; then
        echo " -> 就绪 (HTTP 200, 约 $((i*10)) 秒)"
        break
    fi
    printf "."
    sleep 10
done
echo
sudo journalctl -u inkypi -n 15 --no-pager | sed 's/^/  /'

cat <<EOF

$(say "完成")

下一步:
  1. 打开 http://<Pi的IP>/            进入设置页，把 NASA_SECRET / MINIMAX_API_KEY 填上
     （这两个 key 不可重建，只能重新申请/找小樱拿）
  2. 进 /plugin/duo_calendar          填好日期/语言/版面模式/配色（农历默认开启），点「Update Now」
                                    要右栏显示待办：把 To-Do List 加进播放列表保存，
                                    再回这里把「关联 To-Do 清单」填成第几条（1 = 第一条）
                                    要背景图：填「在线背景图片地址(URL)」+ 选「背景浓度」（推荐淡）
     进 /plugin/meeting                填 ICS 订阅地址 + 会议室名，点「Update Now」
                                      （ICS 地址怎么拿，见 README「真实 ICS 地址怎么填」）
     进 /plugin/todo_list              填 4 栏内容，点「Update Now」
                                      内容要进播放列表：在播放列表页点进实例改完点「Save」
  3. 刷屏约需 2 分钟（渲染 + 两遍全刷各 40s），期间别重复点
  4. 墨水屏规则：两次更新间隔 >=180 秒，至少每 24 小时更新一次
  5. 开机配网：开机后墨水屏会显示一个二维码（约 2 分钟后出现），
     手机扫一下即可重新配无线网络。本机断网时会自动开热点（名字见上面输出）。
     配网页面固定在 http://<本机IP>:8765/，服务名 wifi-setup.service

EOF
