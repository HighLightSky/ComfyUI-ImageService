import asyncio
import base64
from pathlib import Path
from urllib.parse import urlsplit

try:
    import playwright.async_api as playwright_api
except ImportError:
    playwright_api = None

from .errors import ServiceError
from .imaging import open_image, run_image_operation


ASSETS = {
    "/pose_studio/core.js": "text/javascript",
    "/pose_studio/characters.mjs": "text/javascript",
    "/pose_studio/animation.mjs": "text/javascript",
    "/pose_studio/morph_runtime.mjs": "text/javascript",
    "/shared/custom_select.mjs": "text/javascript",
    "/vendor/three/three.module.js": "text/javascript",
    "/vendor/three/OrbitControls.js": "text/javascript",
    "/vendor/three/TransformControls.js": "text/javascript",
    "/pose_studio/assets/pose_studio_makehuman.v2.bin.gz": "application/octet-stream",
    "/pose_studio/textures/skin.png": "image/png",
    "/pose_studio/textures/skin_dummy.png": "image/png",
}
ORIGIN = "http://127.0.0.1"


def validate_render(data, width, height, settings):
    if len(data) > settings.max_image_bytes:
        raise ServiceError(413, "POSE_TOO_LARGE", "姿态参考图大小超限")
    try:
        with open_image(data, settings, png_only=True) as image:
            size = image.size
    except ServiceError:
        raise ServiceError(502, "POSE_RENDER_FAILED", "姿态参考图无效") from None
    if size != (width, height):
        raise ServiceError(502, "POSE_RENDER_FAILED", "姿态参考图尺寸不匹配")


class PoseRenderer:
    def __init__(self, settings):
        self.browser_path = settings.pose_browser_path
        self.asset_root = Path(settings.pose_asset_root)
        self.timeout = settings.pose_render_timeout
        self.cleanup_timeout = settings.cleanup_timeout
        self.settings = settings
        self.driver = None
        self.browser = None

    async def close(self):
        try:
            if self.browser is not None:
                await self.browser.close()
        finally:
            self.browser = None
            if self.driver is not None:
                await self.driver.stop()
                self.driver = None

    async def route(self, route):
        parsed = urlsplit(route.request.url)
        if parsed.scheme != "http" or parsed.netloc != "127.0.0.1":
            await route.abort()
        elif parsed.path == "/":
            await route.fulfill(content_type="text/html", body=Path(__file__).with_suffix(".html").read_bytes())
        elif parsed.path in ASSETS:
            await route.fulfill(content_type=ASSETS[parsed.path], body=(self.asset_root / parsed.path.lstrip("/")).read_bytes())
        else:
            await route.abort()

    async def dispose_context(self, context):
        try:
            async with asyncio.timeout(self.cleanup_timeout):
                if context is not None:
                    await context.close()
                elif self.driver is not None and self.browser is None:
                    await self.close()
        except (TimeoutError, playwright_api.Error):
            try:
                async with asyncio.timeout(self.cleanup_timeout):
                    await self.close()
            except (TimeoutError, playwright_api.Error):
                raise ServiceError(502, "CLEANUP_FAILED", "姿态渲染清理失败，服务正在停止") from None

    async def render(self, pose, width, height):
        if playwright_api is None or not self.browser_path or not Path(self.browser_path).is_file() or not all((self.asset_root / name.lstrip("/")).is_file() for name in ASSETS):
            raise ServiceError(503, "POSE_NOT_READY", "姿态浏览器、依赖或本地资源未就绪")
        context = None
        try:
            async with asyncio.timeout(self.timeout):
                if self.browser is None or not self.browser.is_connected():
                    await self.close()
                    self.driver = await playwright_api.async_playwright().start()
                    self.browser = await self.driver.chromium.launch(
                        executable_path=self.browser_path, headless=True,
                        args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--disable-gpu-shader-disk-cache"],
                    )
                context = await self.browser.new_context(device_scale_factor=1, service_workers="block", accept_downloads=False)
                await context.route("**/*", self.route)
                page = await context.new_page()
                await page.goto(ORIGIN)
                await page.wait_for_function("typeof window.renderPose === 'function'")
                image = await page.evaluate("([pose, width, height]) => window.renderPose(pose, width, height)", [pose, width, height])
                if not isinstance(image, str) or not image.startswith("data:image/png;base64,"):
                    raise ValueError("Invalid pose PNG")
                data = base64.b64decode(image.split(",", 1)[1], validate=True)
                await run_image_operation(validate_render, data, width, height, self.settings)
                return data
        except TimeoutError:
            raise ServiceError(504, "POSE_RENDER_TIMEOUT", "姿态渲染超时") from None
        except (playwright_api.Error, OSError, ValueError):
            raise ServiceError(502, "POSE_RENDER_FAILED", "姿态渲染失败，请检查姿态数据或浏览器配置") from None
        finally:
            cleanup = asyncio.create_task(self.dispose_context(context))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
