import requests
from PIL import Image, ImageEnhance, ImageOps, ImageFilter
from io import BytesIO
import os
import logging
import hashlib
import tempfile
import subprocess
import shutil
import signal

logger = logging.getLogger(__name__)

# PATCH: chromium 渲染硬超时（秒）。
# 上游 take_screenshot() 用 subprocess.run() 且不传 timeout，一旦 chromium 的
# renderer 进程崩溃（例如 V8 快照与 libchromium.so 版本错配导致 V8_Fatal），
# 主进程会永远等不到 renderer，网页请求就此挂死，服务线程被拖垮。
# 这里加进程级超时，并把整组进程（含 renderer / network service 子进程）杀掉。
DEFAULT_SCREENSHOT_TIMEOUT_SEC = 180

def get_image(image_url):
    response = requests.get(image_url, timeout=30)
    img = None
    if 200 <= response.status_code < 300 or response.status_code == 304:
        img = Image.open(BytesIO(response.content))
    else:
        logger.error(f"Received non-200 response from {image_url}: status_code: {response.status_code}")
    return img

def change_orientation(image, orientation, inverted=False):
    if orientation == 'horizontal':
        angle = 0
    elif orientation == 'vertical':
        angle = 90

    if inverted:
        angle = (angle + 180) % 360

    return image.rotate(angle, expand=1)

def resize_image(image, desired_size, image_settings=[]):
    img_width, img_height = image.size
    desired_width, desired_height = desired_size
    desired_width, desired_height = int(desired_width), int(desired_height)

    img_ratio = img_width / img_height
    desired_ratio = desired_width / desired_height

    keep_width = "keep-width" in image_settings

    x_offset, y_offset = 0,0
    new_width, new_height = img_width,img_height
    # Step 1: Determine crop dimensions
    desired_ratio = desired_width / desired_height
    if img_ratio > desired_ratio:
        # Image is wider than desired aspect ratio
        new_width = int(img_height * desired_ratio)
        if not keep_width:
            x_offset = (img_width - new_width) // 2
    else:
        # Image is taller than desired aspect ratio
        new_height = int(img_width / desired_ratio)
        if not keep_width:
            y_offset = (img_height - new_height) // 2

    # Step 2: Crop the image
    image = image.crop((x_offset, y_offset, x_offset + new_width, y_offset + new_height))

    # Step 3: Resize to the exact desired dimensions (if necessary)
    return image.resize((desired_width, desired_height), Image.LANCZOS)

def apply_image_enhancement(img, image_settings={}):
    # Convert image to RGB mode if necessary for enhancement operations
    # ImageEnhance requires RGB mode for operations like blend
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')

    # Each enhance() call blends over the full image, so skip factors of 1.0
    # (the no-op default) to avoid wasted full-frame passes on low-resource devices.
    enhancements = [
        (ImageEnhance.Brightness, image_settings.get("brightness", 1.0)),
        (ImageEnhance.Contrast, image_settings.get("contrast", 1.0)),
        (ImageEnhance.Color, image_settings.get("saturation", 1.0)),
        (ImageEnhance.Sharpness, image_settings.get("sharpness", 1.0)),
    ]
    for enhancer, factor in enhancements:
        if factor != 1.0:
            img = enhancer(img).enhance(factor)

    return img

def compute_image_hash(image):
    """Compute SHA-256 hash of an image."""
    image = image.convert("RGB")
    img_bytes = image.tobytes()
    return hashlib.sha256(img_bytes).hexdigest()

def take_screenshot_html(html_str, dimensions, timeout_ms=None):
    image = None
    try:
        # PATCH: 不再让 timeout_ms 默认为 None，否则下游 chromium 没有超时保护
        if timeout_ms is None:
            timeout_ms = DEFAULT_SCREENSHOT_TIMEOUT_SEC * 1000

        # Create a temporary HTML file
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as html_file:
            html_file.write(html_str.encode("utf-8"))
            html_file_path = html_file.name

        image = take_screenshot(html_file_path, dimensions, timeout_ms)

        # Remove html file
        os.remove(html_file_path)

    except Exception as e:
        logger.error(f"Failed to take screenshot: {str(e)}")

    return image

def _find_chromium_binary():
    """Find the first available Chromium-based binary in system PATH."""
    candidates = ["chromium-headless-shell", "chromium", "chrome"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            logger.debug(f"Found browser binary: {candidate} at {path}")
            return candidate
    return None


def _kill_process_group(proc):
    """杀掉 chromium 及其全部子进程（renderer / network service / zygote）。

    chromium 会 fork 出多个子进程，只 kill 主进程会留下孤儿进程继续吃内存。
    start_new_session=True 让子进程自成一个进程组，因此可以整组 SIGKILL。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def take_screenshot(target, dimensions, timeout_ms=None):
    image = None
    try:
        # Find available browser binary
        browser = _find_chromium_binary()
        if not browser:
            logger.error("No Chromium-based browser found. Install chromium, chromium-headless-shell, or chrome.")
            return None

        # Create a temporary output file for the screenshot
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_file:
            img_file_path = img_file.name

        command = [
            browser,
            target,
            "--headless",
            f"--screenshot={img_file_path}",
            f"--window-size={dimensions[0]},{dimensions[1]}",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--use-gl=swiftshader",
            "--hide-scrollbars",
            "--in-process-gpu",
            "--js-flags=--jitless",
            "--disable-zero-copy",
            "--disable-gpu-memory-buffer-compositor-resources",
            "--disable-extensions",
            "--disable-plugins",
            "--mute-audio",
            "--renderer-process-limit=1",
            "--no-zygote",
            "--no-sandbox",
            # ★ 等字体真正加载完再截图。
            #   @font-face 的字体是异步加载的（simhei/simkai 等中文字体 7~13MB，
            #   从 SD 卡读 + 解析在 Pi Zero 上要 1~3 秒），而 headless 的 --screenshot
            #   不等字体就绪就截 —— 字体处于 font-display:block 期间时文字是
            #   invisible 的，于是整屏只剩边框/背景/分隔线，文字凭空消失。
            #   实测同一份 HTML 连渲 3 次会时好时坏，属于典型竞态。
            #   --virtual-time-budget 让 chromium 推进虚拟时钟并等待挂起的资源加载，
            #   截图前字体就已就位。
            #   注意：本仓库所有插件的 render 模板都不使用 setInterval/setTimeout
            #   （clock 等插件是 Pillow 直绘），虚拟时间快进不会影响内容。
            "--virtual-time-budget=5000",
            "--run-all-compositor-stages-before-draw",
        ]
        if timeout_ms:
            command.append(f"--timeout={timeout_ms}")

        # PATCH: 用 Popen + communicate(timeout) 取代 subprocess.run()。
        # 上游 subprocess.run() 没有 timeout，chromium renderer 一旦崩溃
        # （例如 V8 快照与 libchromium.so 版本错配触发 V8_Fatal），
        # 主进程会永远等下去，网页请求挂死、服务线程被拖垮。
        # 这里同时加进程级超时与整组 kill，避免孤儿进程继续占内存。
        hard_timeout = (timeout_ms / 1000.0) if timeout_ms else DEFAULT_SCREENSHOT_TIMEOUT_SEC

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            logger.error(f"Failed to launch browser: {str(e)}")
            return None

        try:
            stdout, stderr = proc.communicate(timeout=hard_timeout)
        except subprocess.TimeoutExpired:
            logger.error(
                f"Browser screenshot timed out after {hard_timeout:.0f}s; killing process group"
            )
            _kill_process_group(proc)
            try:
                proc.communicate(timeout=10)
            except Exception:
                pass
            try:
                os.remove(img_file_path)
            except OSError:
                pass
            return None

        # Check if the process failed or the output file is missing
        if proc.returncode != 0 or not os.path.exists(img_file_path):
            logger.error(f"Failed to take screenshot (return code: {proc.returncode})")
            if stderr:
                logger.error(
                    f"Browser stderr: {stderr.decode('utf-8', 'replace')[-500:]}"
                )
            try:
                os.remove(img_file_path)
            except OSError:
                pass
            return None

        # Load the image using PIL
        with Image.open(img_file_path) as img:
            image = img.copy()

        # Remove image files
        os.remove(img_file_path)

    except Exception as e:
        logger.error(f"Failed to take screenshot: {str(e)}")

    return image

def pad_image_blur(img: Image, dimensions: tuple[int, int]) -> Image:
    bkg = ImageOps.fit(img, dimensions)
    bkg = bkg.filter(ImageFilter.BoxBlur(8))
    img = ImageOps.contain(img, dimensions)

    img_size = img.size
    bkg.paste(img, ((dimensions[0] - img_size[0]) // 2, (dimensions[1] - img_size[1]) // 2))
    return bkg
