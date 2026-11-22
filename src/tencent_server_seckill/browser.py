import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

from .storage import DATA_DIR, log

try:
    from playwright.async_api import Browser, BrowserContext
    from playwright.async_api import TimeoutError as TimeoutError
    from playwright.async_api import async_playwright as async_playwright
except ImportError:
    print("缺少 playwright，请执行 uv sync --locked --no-dev")
    sys.exit(1)


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


# 启动顺序：先用本机的，都没装就回落到 playwright 下载的 chromium
BROWSER_CHANNELS = ["chrome", "msedge", None]

BROWSER_LABEL = {
    "chrome": "本机 Chrome",
    "msedge": "本机 Edge",
    None: "playwright chromium",
}

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
BROWSERS_PATH = DATA_DIR / ".playwright-browsers"


def ensure_chromium() -> None:
    """Prepare the project browser cache before starting Playwright."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_PATH)
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdin=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Chromium 安装失败，下次启动会重试。{exc}", file=sys.stderr)


async def launch_browser(pw, headless=True, extra_args=None):
    """按 BROWSER_CHANNELS 顺序试，谁能起就用谁。"""
    args = LAUNCH_ARGS + list(extra_args or [])
    last = None

    for channel in BROWSER_CHANNELS:
        kwargs = {"headless": headless, "args": args}
        if channel:
            kwargs["channel"] = channel
        try:
            browser = await pw.chromium.launch(**kwargs)
            log(f"使用浏览器: {BROWSER_LABEL[channel]}")
            return browser
        except Exception as e:
            last = e
            if channel is None:
                log(f"启动失败: {e}")
                log(f"项目内 Chromium 缓存目录: {BROWSERS_PATH}")

    raise RuntimeError(f"没有可用的浏览器: {last}")


async def new_api_context(browser: Browser, storage_state: Path) -> BrowserContext:
    """Apply the Windows proxy selected for requests to the activity API."""
    server = os.environ.get("TENCENT_SECKILL_API_PROXY")
    return await browser.new_context(
        storage_state=storage_state,
        user_agent=UA,
        proxy={"server": server} if server else None,
    )


# 前端的 x-csrf-token 是动态生成的，得 hook 住 fetch/XHR 才能拿到。
#
# 实测教训：活动页这一版用 `new Headers({...})` 传头，旧版只判断 h['x-csrf-token']
# 对 Headers 实例取不到（Headers 的下标是空的，只能 h.get()），导致
# window.__latestCsrf 常年为 null，请求带空 token 发出去被服务端回 400 CSRF-ERROR，
# 再被误判成"登录态失效"，于是不停要求重新扫码。
# 所以这里三种形态都要认：Headers 实例、普通对象、[[k,v],...] 数组。
CSRF_HOOK_JS = (files("tencent_server_seckill") / "scripts" / "csrf_hook.js").read_text(
    encoding="utf-8"
)


# ---------- CSRF 兜底抓取 ----------
# JS hook 依赖页面还在跑我们注入的代码，页面内部再用 service worker / iframe
# 之类绕过时就会漏。Playwright 在协议层能看到每一条请求头，所以再从这一侧记一份：
# 只要页面自己发过一次带 x-csrf-token 的请求，这里就一定有值。
def attach_csrf_sniffer(page):
    """在 page 上挂一个请求头嗅探器，把抓到的 csrf 记到 page._csrf_sniff。"""
    page._csrf_sniff = None
    page._csrf_sniff_urls = []

    def _on_request(req):
        try:
            v = req.headers.get("x-csrf-token")
            if v:
                page._csrf_sniff = v
                if len(page._csrf_sniff_urls) < 20:
                    page._csrf_sniff_urls.append(req.url[:120])
        except Exception:
            pass

    try:
        page.on("request", _on_request)
    except Exception:
        pass
    return page


async def ensure_csrf(page, force=False):
    """保证 window.__latestCsrf 有值。

    顺序：页面自己的 -> 协议层嗅探到的 -> 从 cookie 现算 g_tk。
    拿不到就返回 None，调用方不要拿空 token 硬发请求（空 token 会换来
    400 CSRF-ERROR，再被误判成登录失效，这才是"反复要求扫码"的根源）。

    force=True：不信页面缓存，强制按当前 skey 用 g_tk 重算。
    扫码登录后 skey 会换新，而 window.__latestCsrf 里存的是登录**前**
    （匿名时）的旧 token。页面若没自行刷新，旧 token 就一直在，请求会被
    回 CSRF-ERROR，登录检测永远等不到 code=0 —— 这就是"扫完码怎么都
    登不上"的根因。13:08 那次能成功纯属页面碰巧自己刷新了。
    """
    if force:
        calc = await _csrf_from_skey(page)
        if calc:
            try:
                await page.evaluate("(v) => { window.__latestCsrf = v; }", calc)
            except Exception:
                pass
            return calc
        # 重算不出来（页面里没 skey）就用嗅探到的，仍是拿不到才返回 None
        return getattr(page, "_csrf_sniff", None)

    try:
        cur = await page.evaluate("() => window.__latestCsrf || null")
        if cur:
            return cur
    except Exception:
        pass

    sniffed = getattr(page, "_csrf_sniff", None)
    if sniffed:
        try:
            await page.evaluate("(v) => { window.__latestCsrf = v; }", sniffed)
        except Exception:
            pass
        return sniffed

    # 最后兜底：按前端的 g_tk 算法，从 skey/p_skey 现算一个
    calc = await _csrf_from_skey(page)
    if calc:
        try:
            await page.evaluate("(v) => { window.__latestCsrf = v; }", calc)
        except Exception:
            pass
        return calc
    return None


async def _csrf_from_skey(page):
    """在页面里读 document.cookie 的 skey/p_skey，按腾讯前端 g_tk 算法算出
    csrf token。实测与页面自己发出的 x-csrf-token 完全一致（2120550790）。

    skey 的 httpOnly=False，所以 JS 能读到；万一读不到返回 None，调用方
    自行降级，绝不拿空 token 去请求。
    """
    try:
        return await page.evaluate("""
            () => {
                const cs = (document.cookie || '').split(';');
                let skey = null;
                for (const c of cs) {
                    const i = c.indexOf('=');
                    if (i < 0) continue;
                    const k = c.slice(0, i).trim();
                    if (k === 'skey' || k === 'p_skey') { skey = c.slice(i + 1); break; }
                }
                if (!skey) return null;
                let e = 5381;
                for (let i = 0; i < skey.length; i++) e += (e << 5) + skey.charCodeAt(i);
                return String(2147483647 & e);
            }
        """)
    except Exception:
        return None


async def _report_state_cookies(ctx):
    """数一下当前 context 里的关键 cookie，返回给人看的描述。

    用来避免"明明没存到登录态却打印已保存"的假象——用户看到这句话就会去
    关浏览器，结果下次又得重扫，这是他反复扫码的最直接来源。
    """
    try:
        cookies = await ctx.cookies()
    except Exception as e:
        return f"读取失败: {str(e)[:40]}"
    names = {c.get("name") for c in cookies}
    keys = [k for k in ("skey", "p_skey", "uin") if k in names]
    if keys:
        return f"{len(cookies)} 条，含 {'/'.join(keys)}"
    return f"{len(cookies)} 条，缺 skey/uin（登录态不完整）"
