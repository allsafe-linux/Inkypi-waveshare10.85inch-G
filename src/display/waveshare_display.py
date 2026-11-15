import inspect
import importlib
import logging
import sys

from display.abstract_display import AbstractDisplay
from PIL import Image
from pathlib import Path

logger = logging.getLogger(__name__)


def split_image_for_bi_color_epd(image):
    """
    Convert image into two 1-bit layers for bi-color (black and red) e-paper displays.
    """
    black = (0, 0, 0)
    white = (255, 255, 255)
    red = (255, 0, 0)

    palette_data = [*black, *white, *red]
    palette_img = Image.new('P', (1, 1))
    palette_img.putpalette(palette_data)

    indexed_img = image.quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG)
    black_layer = indexed_img.point(lambda p: 0 if p == 0 else 1, mode='1')
    red_layer = indexed_img.point(lambda p: 0 if p == 2 else 1, mode='1')
    return black_layer, red_layer


class WaveshareDisplay(AbstractDisplay):
    """
    Handles Waveshare e-paper display dynamically based on device type.

    This class loads the appropriate display driver dynamically based on the
    `display_type` specified in the device configuration, allowing support for
    multiple Waveshare EPD models.

    The module drivers are in display.waveshare_epd.

    ------------------------------------------------------------------
    10.85" (G) 四色屏适配（本地定制，非上游）
    ------------------------------------------------------------------
    上游原版对本屏有两处致命缺陷，这里做了修正：

    1. 分辨率：上游取 `w, h = width, height`，而 10.85" (G) 面板由 **两颗
       SSD1681** 各负责半屏，驱动的 `width` 是「单 IC 宽度」(680)，真实分辨率
       是 `width * 2` = 1360。上游会把分辨率写成 680x480，画面被横向压掉一半。
       → 修正为 `w = width * DUAL_IC_WIDTH_MULTIPLIER`。

    2. 方向：HAT 是倒着装的，直接送屏会上下颠倒 180°。
       → 送屏前统一旋转 `HAT_ROTATION_DEG`。

    另外：4 色屏不支持局部刷新（见 Waveshare FAQ），因此这里恒走全刷路径。
    """

    # ----- Class constants -----
    HAT_ROTATION_DEG = 180             # HAT 物理装反了，旋转修正
    DUAL_IC_WIDTH_MULTIPLIER = 2       # 10.85" G 是双 SSD1681，各管半屏

    def initialize_display(self):

        """
        Initializes the Waveshare display device.

        Retrieves the display type from the device configuration and dynamically
        loads the corresponding Waveshare EPD driver from display.waveshare_epd.

        Raises:
            ValueError: If `display_type` is missing or the specified module is
                        not found.
        """

        logger.info("Initializing Waveshare display")

        # get the device type which should be the model number of the device.
        display_type = self.device_config.get_config("display_type")
        logger.info(f"Loading EPD display for {display_type} display")

        if not display_type:
            raise ValueError("Waveshare driver but 'display_type' not specified in configuration.")

        # Construct module path dynamically - e.g. "display.waveshare_epd.epd7in3e"
        module_name = f"display.waveshare_epd.{display_type}"

        # Workaround for some Waveshare drivers using 'import epdconfig' causing import errors
        epd_dir = Path(__file__).parent / "waveshare_epd"
        if str(epd_dir) not in sys.path:
            sys.path.insert(0, str(epd_dir))

        try:
            # Dynamically load module
            epd_module = importlib.import_module(module_name)
            self.epd_display = epd_module.EPD()
            # Workaround for init functions with inconsistent casing
            self.epd_display_init = getattr(self.epd_display, "Init", getattr(self.epd_display, "init", None))

            if not callable(self.epd_display_init):
                raise AttributeError("No Init/init method found")

            self.epd_display_init()

            display_args_spec = inspect.getfullargspec(self.epd_display.display)
        except ModuleNotFoundError:
            raise ValueError(f"Unsupported Waveshare display type: {display_type}")
        except AttributeError:
            raise ValueError(f"Display does not support required methods: {display_type}")

        # display(image)        -> 1 个业务参数 -> 四色屏（驱动内部做 4 色调色板量化）
        # display(black, red)   -> 2 个业务参数 -> 双色屏（需要拆成两层）
        self.bi_color_display = len(display_args_spec.args) > 2

        # update the resolution directly from the loaded device context
        if not self.device_config.get_config("resolution"):
            # PATCH: 双 IC 面板的真实宽度是驱动 width 的两倍（10.85" G: 680 -> 1360）
            w = int(self.epd_display.width) * self.DUAL_IC_WIDTH_MULTIPLIER
            h = int(self.epd_display.height)
            resolution = [w, h] if w >= h else [h, w]
            self.device_config.update_value(
                "resolution",
                resolution,
                write=True)

    def display_image(self, image, image_settings=[]):

        """
        Displays an image on the Waveshare display.

        The image has been processed by adjusting orientation, resizing, and converting it
        into the buffer format required for e-paper rendering.

        Args:
            image (PIL.Image): The image to be displayed.
            image_settings (list, optional): Additional settings to modify image rendering.

        Raises:
            ValueError: If no image is provided.
        """

        logger.info("Displaying image to Waveshare display.")
        if not image:
            raise ValueError(f"No image provided.")

        # PATCH: rotate 180° to match HAT physical orientation
        image = image.rotate(self.HAT_ROTATION_DEG)

        # Assume device was in sleep mode.
        self.epd_display_init()

        # Clear residual pixels before updating the image.
        self.epd_display.Clear()

        # Display the image on the WS display.
        # 四色：单缓冲，驱动内部量化为 4 色；双色：拆成黑白 + 红两层
        self.epd_display.display(*self._prepare_display_layers(image))

        # Put device into low power mode (EPD displays maintain image when powered off)
        logger.info("Putting Waveshare display into sleep mode for power saving.")
        self.epd_display.sleep()

    def _prepare_display_layers(self, image):
        """
        按屏类型准备传给 driver.display() 的缓冲列表。

        - 四色屏（如 10.85" G）：返回 1 个缓冲，驱动自己做 4 色调色板量化
        - 双色屏（如 7.5" HD V2）：返回 2 个缓冲（黑白层 + 红层）

        调用方统一 `self.epd_display.display(*layers)`，两条路径一个调用点。
        """
        if self.bi_color_display:
            black_layer, red_layer = split_image_for_bi_color_epd(image)
            return [
                self.epd_display.getbuffer(black_layer),
                self.epd_display.getbuffer(red_layer),
            ]
        # 四色路径：单缓冲，驱动内部处理调色板
        return [self.epd_display.getbuffer(image)]
