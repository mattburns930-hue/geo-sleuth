"""Shared Chrome / Playwright Chromium launch and actionable setup errors."""
from __future__ import annotations

from _net import resolve_proxy


# Reverse-search upload pages: URL + labels/selectors tried in order when no file input is in the DOM
# (engines often create the control on click). Shared by revimg.py and doctor.py.
UPLOAD_PAGES = {
    "baidu": ("https://graph.baidu.com/pcpage/index?tpl_from=pc",
              ("上传图片", "本地上传", "选择文件", "拍照识图", "css=[class*='upload']", "css=[class*='camera']")),
    "yandex": ("https://yandex.com/images/search?rpt=imageview",
               ("Select a file", "Choose file", "Выбрать файл", "css=[class*='upload']", "css=[aria-label*='image']")),
}
FILE_INPUTS = ("input[type=file]", "input[accept*='image']", "input[name='image']", "input[name='file']")
CAPTCHA_URL = ("wappass.baidu.com", "captcha", "/verify", "showcaptcha", "checkcaptcha")
CAPTCHA_TEXT = ("安全验证", "百度安全验证", "请完成验证", "拖动滑块", "verify you are human", "are you a robot",
                "confirm that you're not a robot", "unusual traffic")


def watch_failures(page) -> list[str]:
    """Collect hosts of sub-requests that fail or return ≥400 while the page loads (blocked script/CDN hosts)."""
    failed: list[str] = []

    def host(u: str) -> str:
        return u.split("/")[2] if "://" in u else u

    page.on("requestfailed", lambda r: failed.append(host(r.url)))
    page.on("response", lambda r: failed.append(host(r.url)) if r.status >= 400 else None)
    return failed


async def missing_upload_reason(page, failed: list[str], engine: str) -> str:
    """Why no upload control was found: captcha, blocked page resources, empty page, or a real redesign."""
    why = await page_block_reason(page)
    if why:
        return why
    try:
        text = (await page.inner_text("body", timeout=3000)).strip()
    except Exception:  # noqa: BLE001
        text = ""
    if failed:
        hosts = sorted(set(failed))
        return (f"network: {len(failed)} page resource(s) failed to load from {', '.join(hosts[:4])}"
                f"{' …' if len(hosts) > 4 else ''}, so the page never rendered its upload control "
                "(proxy/allowlist/firewall issue, not a redesign)")
    if len(text) < 20:
        return f"{engine} page rendered empty (scripts did not run); retry, or check network access to its CDN hosts"
    return f"page loaded but no upload control matched ({engine} changed its upload UI): update FILE_INPUTS / UPLOAD_PAGES in _browser.py"


async def open_page(page, url: str, timeout: int = 60000) -> str | None:
    """Navigate; return None on success, or an error that says *why* (network vs HTTP status)."""
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).splitlines()[0]
        return f"network unreachable: could not load {url.split('/')[2]} ({msg[:140]}); check DNS/proxy/allowlist, not the page"
    if resp is not None and resp.status >= 400:
        return f"HTTP {resp.status} from {url.split('/')[2]}: blocked or rate-limited (not a page redesign)"
    return None


async def page_block_reason(page) -> str | None:
    """Captcha / verification interstitials, which look like 'no upload box' but need a different fix."""
    url = page.url.lower()
    try:
        text = (await page.inner_text("body", timeout=3000))[:4000].lower()
    except Exception:  # noqa: BLE001
        text = ""
    if any(k in url for k in CAPTCHA_URL) or any(k.lower() in text for k in CAPTCHA_TEXT):
        return "captcha/verification page: open it once in a normal browser, retry later, or use another engine"
    return None


async def find_upload(page, buttons: tuple[str, ...] = (), timeout_ms: int = 4000):
    """Return (kind, handle) for the upload control: a file input, or a button that opens a file chooser.

    kind is "input" or "chooser"; (None, None) if neither exists. Tries several selectors because engines
    rename classes often, and falls back to clicking visible upload buttons and catching the file chooser.
    """
    for sel in FILE_INPUTS:
        el = await page.query_selector(sel)
        if el:
            return "input", el
    for label in buttons:
        loc = page.get_by_text(label, exact=False) if not label.startswith(("css=", "[", ".", "#")) else page.locator(label.removeprefix("css="))
        try:
            if not await loc.count():
                continue
            async with page.expect_file_chooser(timeout=timeout_ms) as fc:
                await loc.first.click(timeout=timeout_ms)
            return "chooser", await fc.value
        except Exception:  # noqa: BLE001
            for sel in FILE_INPUTS:  # some buttons inject the input instead of opening a chooser
                el = await page.query_selector(sel)
                if el:
                    return "input", el
    return None, None


async def upload_file(kind: str, handle, path: str) -> None:
    if kind == "chooser":
        await handle.set_files(path)
    else:
        await handle.set_input_files(path)


async def launch_browser(playwright, proxy: str | None = None):
    value = resolve_proxy(proxy)
    options = {"headless": True, "timeout": 15000,
               "args": ["--disable-blink-features=AutomationControlled"]}
    if value:
        options["proxy"] = {"server": value.replace("socks5h://", "socks5://")}
    else:
        options["args"].append("--no-proxy-server")
    errors = []
    for channel in ("chrome", None):
        try:
            browser = await playwright.chromium.launch(
                **({"channel": channel} if channel else {}), **options)
            return browser, "Google Chrome" if channel else "Playwright Chromium"
        except Exception as exc:
            errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0]}")
    raise RuntimeError(
        "Neither Google Chrome nor Playwright Chromium could start. "
        "Run `uvx playwright install chromium` and retry; on Linux, missing system libraries "
        "may require `uvx playwright install --with-deps chromium`. "
        + " | ".join(errors))
