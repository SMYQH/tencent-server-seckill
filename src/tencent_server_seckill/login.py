import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .browser import (
    CSRF_HOOK_JS,
    UA,
    apply_stealth,
    attach_csrf_sniffer,
    ensure_csrf,
    launch_browser,
    new_api_context,
)
from .console import Fore, c, log
from .settings import ACTIVITY_URL, API_HOST
from .storage import STATE_PATH, _state_missing_skey, _state_summary

VIP_URL = "https://act-api.cloud.tencent.com/user/get-vip-info"


# ---------- 登录 ----------
_NOT_LOGGED_CODES = {"NOT-LOGINED", "NOT_LOGINED", "NOTLOGINED", "ERROR", "FAIL", "UNAUTHORIZED"}
# 只有明确表示成功的 code 才算已登录，其余未识别的一律按未登录处理
_OK_CODES = {"OK", "SUCCESS", "SUCC"}


def _login_state(r):
    """判断 get-vip-info 的返回，'ok' / 'not-logged' / 'csrf' / 'unknown'。

    不能只看 code != 'NOT-LOGINED'：请求失败时前端 fetch 会被 catch 成
    {code: -1}，那不等于 NOT-LOGINED，判成"登录成功"就会误报。

    CSRF-ERROR 单独成一类，但它**不代表已登录**。三方实测：
      带对 csrf  -> code=0
      csrf 陈旧/错误 -> CSRF-ERROR
      连 skey 都没有 -> NOT-LOGINED
    它的意思是「请求带的 csrf 与当前 skey 对不上」，必须拿当前 skey 重算
    token 才能消掉（见 _check_login 的 force 重算）。既不能判成功（会存下
    假登录态），也不能判失效（会逼人白重扫）。
    """
    if not isinstance(r, dict) or "code" not in r:
        return "unknown"
    code = r["code"]
    if code is None or isinstance(code, bool):
        return "unknown"
    if isinstance(code, (int, float)):
        # 只认 0 为成功。正数可能是业务码（1100205 系统繁忙、408 限流、
        # 200 这种 HTTP 风格值），以前一律判 ok，等于把"系统繁忙"当成
        # "登录成功"—— 这是假成功的另一个入口，必须堵掉。
        if code == 0:
            return "ok"
        return "not-logged" if code < 0 else "unknown"
    text = str(code).strip().upper()
    if not text:
        return "unknown"
    if "CSRF" in text:
        return "csrf"  # token 对不上，skey 其实在
    if text in _NOT_LOGGED_CODES:
        return "not-logged"
    if text in _OK_CODES or text == "0":
        return "ok"
    # 不认识的码一律 unknown：既不报成功（防假成功），也不报失效（防误逼重扫）
    return "unknown"


def _i32(x):
    """模拟 JS 的 ToInt32：截到 32 位并转成有符号。"""
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def gtk_of(skey):
    """按腾讯前端的 g_tk 算法从 skey 算出 csrf token（纯 Python，不开页面）。

    必须逐位对齐 JS，不能只写 `e += (e << 5) + ord(c)`：
      · `e << 5` 在 JS 里先 ToInt32 再左移，会溢出成负数；
      · e 本身是 double，累加几十轮就超过 2^53 会丢精度，Python int 不会。
    所以这里 e 用 float 走 IEEE754 加法和 ToInt32 截断，与浏览器完全一致。
    实测：305 组随机串（长度 1~300，含空串）与页面 JS 结果 100% 相同。
    """
    if not skey:
        return None
    e = 5381.0
    for ch in skey:
        e = e + float(_i32(_i32(int(e)) << 5)) + float(ord(ch))
    return str(2147483647 & _i32(int(e)))


def _domain_matches(cookie_domain, host):
    """这条 cookie 会不会被发到 host（RFC 6265 的 domain-match）。

    Playwright 里 domain 带前导点 = 域 cookie（子域也发），不带 = host-only
    cookie（只发给这个主机本身）。
    """
    d = (cookie_domain or "").lstrip(".").lower()
    h = (host or "").lower()
    if not d or not h:
        return False
    return h == d or h.endswith("." + d)


def _pick_skey(cookies, host):
    """在 cookie 列表里挑出「host 真的会收到」的那条 skey。挑不到返回 None。"""
    for want in ("skey", "p_skey"):
        cands = [
            c
            for c in cookies
            if c.get("name") == want and c.get("value") and _domain_matches(c.get("domain"), host)
        ]
        if not cands:
            continue
        # 同名多条时：host 精确匹配优先，其次域名最长（最具体）的那条
        cands.sort(
            key=lambda c: (
                0 if (c.get("domain") or "").lstrip(".").lower() == host else 1,
                -len((c.get("domain") or "").lstrip(".")),
            )
        )
        return cands[0]
    return None


async def api_skey(ctx, url=None):
    """取「API 主机真正会收到」的那条 skey cookie。

    【踩过的坑】.state.json 里同时躺着三条 skey：
        .qcloud.com         国际站
        .cloud.tencent.cn   中国站 .cn
        .cloud.tencent.com  国内主站（act-api.cloud.tencent.com 用的就是它）
    以前从整个 cookie jar 里取第一条（.qcloud.com）算 csrf，那条 cookie 根本
    不会发到 act-api.cloud.tencent.com，用它算出来的 token 必然对不上。
    2026-11-22 实测（同一个 context，只换 skey）：
        .qcloud.com 那条        -> {"code":"CSRF-ERROR","msg":"CSRF 校验失败"}
        .cloud.tencent.cn 那条  -> {"code":"CSRF-ERROR","msg":"CSRF 校验失败"}
        .cloud.tencent.com 那条 -> {"code":0,"msg":"ok"}   <- 只有这条能用

    所以这里按 URL 作用域取：ctx.cookies(url) 只返回浏览器会发给这个 URL 的
    cookie，跨域的 skey 根本不在列表里。
    """
    url = url or VIP_URL
    host = urlsplit(url).hostname or API_HOST
    cookies = []
    try:
        cookies = await ctx.cookies(url)
    except Exception:
        cookies = []
    if not cookies:
        # 兜底：老版本 playwright 不支持按 URL 过滤时取全量，后面再按域名筛
        try:
            cookies = await ctx.cookies()
        except Exception:
            return None
    return _pick_skey(cookies, host)


async def csrf_from_context(ctx, url=None):
    """从 API 作用域的 skey 现算 csrf，不依赖页面、也不加载页面。

    作用域很重要：全量 cookie jar 里有三条 skey，只有发到 API 主机的
    那条算出来的 token 服务端才认（详见 api_skey 的注释）。
    """
    ck = await api_skey(ctx, url)
    if not ck:
        return None
    return gtk_of(ck.get("value"))


async def verify_state(ctx, timeout=15):
    """只打 get-vip-info 校验登录态，**绝不加载任何页面**。返回 (state, r)。

    【为什么不能用页面校验】
    实测（2026-11-12）：拿一份失效 skey 打开 cloud.tencent.com，页面自己请求
    /services/ajax/get_user_info 时，服务端会回 Set-Cookie: skey=; uin=; tinyid=;
    —— 三个登录 cookie 当场被清空（21 条 -> 18 条，skey/uin/tinyid 全没）。
    也就是说「打开活动页校验登录态」这个动作本身在销毁登录态：本来只是暂时
    失效、重试或稍后还能用的，被这么一验就彻底没了，只能重扫。
    有效登录态不受影响（实测 33 -> 34 条，三个都在），但失效时就是雪上加霜。

    只打接口则完全不动 cookie（实测前后条数一致）。csrf 用 g_tk(skey) 现算，
    与页面 hook 抓到的值完全一致（同一算法，305 组实测逐位相同）。
    """
    ck = await api_skey(ctx, VIP_URL)
    if not ck:
        return "unknown", {"__err": f"context 里没有 {API_HOST} 作用域的 skey"}
    gtk = gtk_of(ck.get("value"))
    if not gtk:
        return "unknown", {"__err": "skey 是空的"}
    log(f"校验登录态：用 {ck.get('domain')} 的 skey 算 csrf（token={gtk}）")
    try:
        resp = await ctx.request.post(
            VIP_URL,
            data="{}",
            headers={"Content-Type": "application/json", "x-csrf-token": gtk},
            timeout=int(timeout * 1000),
        )
        r = await resp.json()
    except Exception as e:
        return "unknown", {"__err": str(e)[:80]}
    state = _login_state(r)
    if state == "csrf":
        # token 与服务端当前 skey 对不上。skey 可能刚换过，按当前值重算一次再试。
        await asyncio.sleep(0.5)
        ck2 = await api_skey(ctx, VIP_URL)
        tok2 = gtk_of(ck2.get("value")) if ck2 else None
        if tok2 and tok2 != gtk:
            try:
                resp2 = await ctx.request.post(
                    VIP_URL,
                    data="{}",
                    headers={"Content-Type": "application/json", "x-csrf-token": tok2},
                    timeout=int(timeout * 1000),
                )
                r2 = await resp2.json()
            except Exception:
                r2 = None
            if r2 is not None:
                log(f"重算 csrf 后再试一次（token={tok2}）")
                return _login_state(r2), r2
    return state, r


async def verify_state_file(state_path=None):
    """用磁盘上的 .state.json 开一个无头 context 做只读校验，用完即关。

    不需要页面、不会碰 cookie，校验完把 context 扔掉，不落盘。
    返回 (state, r, ctx_cookie_count)。
    """
    from playwright.async_api import async_playwright

    path = Path(state_path) if state_path else STATE_PATH
    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await launch_browser(pw, headless=True)
        ctx = await new_api_context(browser, path)
        state, r = await verify_state(ctx)
        try:
            n = len(await ctx.cookies())
        except Exception:
            n = -1
        await ctx.close()
        return state, r, n
    except Exception as e:
        return "unknown", {"__err": str(e)[:80]}, -1
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


async def _check_login(page):
    """无害验证：get-vip-info 查询接口，绝不产生订单。返回 (state, r)

    state: 'ok' / 'not-logged' / 'csrf' / 'unknown'
    """
    await ensure_csrf(page)
    r = await page.evaluate(
        """
        (url) => fetch(url, {method:'POST',
            headers:{'Content-Type':'application/json','x-csrf-token':window.__latestCsrf||''},
            credentials:'include', body:'{}'})
            .then(r => r.json())
            .catch(e => ({__err: String(e)}))
    """,
        VIP_URL,
    )
    state = _login_state(r)
    if state == "csrf":
        # 换成**当前** skey 重算 csrf 再试一次。
        # 这里必须 force：扫码后 skey 已换新，但 window.__latestCsrf 还是
        # 登录前抓到的旧 token，不强制重算的话重试多少次都是 CSRF-ERROR。
        await asyncio.sleep(0.4)
        await ensure_csrf(page, force=True)
        r2 = await page.evaluate(
            """
            (url) => fetch(url, {method:'POST',
                headers:{'Content-Type':'application/json','x-csrf-token':window.__latestCsrf||''},
                credentials:'include', body:'{}'})
                .then(r => r.json())
                .catch(e => ({__err: String(e)}))
        """,
            VIP_URL,
        )
        state2 = _login_state(r2)
        if state2 != "csrf":
            return state2, r2
    return state, r


async def _wait_csrf(page, tries=50):
    """等 CSRF token 就绪。返回 token 或 None，别拿空 token 去请求。

    腾讯云活动页加载后还会自我重定向/重载，导航期间 evaluate 会报
    "Execution context was destroyed"，吞掉重试。"""
    for _ in range(tries):
        try:
            tok = await ensure_csrf(page)
            if tok:
                return tok
        except Exception:
            pass  # 导航中，等页面稳下来再试
        await asyncio.sleep(0.2)
    return None


async def _save_state(ctx):
    """存登录态，并读回校验关键 cookie 是否真的落盘。

    返回 (ok, desc)。desc 给人看，形如 "21 条，含 skey/uin"。

    两条保护：
    1. 先写临时文件、校验通过才替换，避免写一半把原文件搞坏。
    2. 新快照缺 skey/uin 而原文件有，就保留原文件 —— 否则一次网络抖动
       就能把用户刚扫好的登录态覆盖成空壳，下次又得重扫。
    """
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    try:
        await ctx.storage_state(path=str(tmp))
    except Exception as e:
        log(f"保存登录态失败: {e}")
        return False, f"写入失败: {str(e)[:40]}"

    def _keys_of(path):
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            return None, 0
        cs = [c for c in (d.get("cookies") or []) if isinstance(c, dict)]
        names = {c.get("name") for c in cs}
        return [k for k in ("skey", "p_skey", "uin") if k in names], len(cs)

    new_keys, n = _keys_of(tmp)
    if new_keys is None:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False, "读回失败（临时文件不可解析）"

    if not new_keys and STATE_PATH.exists():
        old_keys, old_n = _keys_of(STATE_PATH)
        if old_keys:
            try:
                tmp.unlink()
            except Exception:
                pass
            log(
                f"本次快照缺 skey/uin（{n} 条），保留原登录态（{old_n} 条，含 {'/'.join(old_keys)}）"
            )
            return True, f"保留原登录态（{old_n} 条，含 {'/'.join(old_keys)}）"

    try:
        os.replace(str(tmp), str(STATE_PATH))
    except Exception as e:
        return False, f"替换失败: {str(e)[:40]}"

    if new_keys:
        return True, f"{n} 条，含 {'/'.join(new_keys)}"
    return False, f"{n} 条，缺 skey/uin"


async def _has_login_cookie(ctx):
    """context 里有没有真正的登录凭证（skey/p_skey + uin）。

    这才是"登录成功"的硬标准。光看 get-vip-info 的返回不可靠：
    code 可能是 200 之类的 HTTP 风格值，被当成成功，但 cookie 根本没下发。
    实测用户真实 .state.json：19 条 cookie、有 loginType=wx / qcloud_uid，
    却一个 skey 都没有 —— 就是"接口说成功、cookie 没到位"时存下的空壳。
    """
    try:
        # 只看 API 主机会收到的那批 cookie：别的域名的 skey（比如 .qcloud.com）
        # 对这个接口不算数，认了也是假登录态
        names = {ck.get("name") for ck in await ctx.cookies(VIP_URL)}
    except Exception:
        return False, set()
    has = ("skey" in names or "p_skey" in names) and "uin" in names
    return has, names


async def _wait_login_cookies(ctx, timeout=12.0):
    """等 skey/uin 真正下发到 context 再往下走。

    扫码成功到 cookie 落地之间有几秒延迟，固定 sleep 1.5s 经常踩空，
    存出来就是一份没有 skey 的空壳 —— 用户看到"已保存"就关浏览器，
    下次还得重扫。这里改成盯着 ctx.cookies() 直到 skey 出现。
    """
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        ok, _ = await _has_login_cookie(ctx)
        if ok:
            return True
        await asyncio.sleep(0.5)
    return False


async def do_login():
    """弹浏览器扫码，检测到登录后存 cookie。"""
    c("=" * 56, Fore.YELLOW)
    c("扫码登录", Fore.YELLOW)
    c("=" * 56, Fore.YELLOW)
    c("浏览器即将弹出（仅登录用）", Fore.CYAN)
    c("请在弹出窗口里：点右上角【登录】→ 微信扫码", Fore.CYAN)
    c("（本机已保存过登录态可跳过本步骤）", Fore.WHITE)
    c("扫码成功后会等几秒让 cookie 落地，这期间请不要关闭浏览器。", Fore.WHITE)

    pw = None
    browser = None
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await launch_browser(pw, headless=False)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900}, user_agent=UA)
        # 反检测交给 playwright-stealth 整套（navigator.webdriver 只是其中一条）
        await apply_stealth(ctx)
        await ctx.add_init_script(CSRF_HOOK_JS)
        page = await ctx.new_page()
        page.set_default_timeout(30000)
        attach_csrf_sniffer(page)

        log("打开活动页...")
        await page.goto(ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
        if not await _wait_csrf(page):
            log("未捕获到 CSRF token，登录检测可能不准确")

        log("请扫码... (10分钟超时)")
        deadline = asyncio.get_event_loop().time() + 600
        ok = False
        hinted = False
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(3)
            try:
                state, r = await _check_login(page)
                has_cookie, names = await _has_login_cookie(ctx)

                # 唯一可靠的成功判据：服务端认（get-vip-info 返回 OK）。
                #
                # 曾经用「cookie 里有 skey+uin」当硬标准，实测证明会有假阳性：
                # 扫码后 skey 已经下发，但微信那步没点"确认登录"，服务端照样
                # 回 NOT-LOGINED、页面右上角照样是"登录"按钮、进 console 会被
                # 打回登录页。拿这种 skey 存 state 就是存了个假登录态。
                if state == "ok":
                    ok = True
                    break

                # 【教训】这里曾经加过一条判据：「有 skey/uin + 服务端回
                # CSRF-ERROR = 已登录」。它是 6cea7e9 引入的，也是这次
                # 「扫码怎么都登不上」的根源之一，已彻底删除。
                #
                # CSRF-ERROR 的真实含义是「请求带的 csrf 与当前 skey 对不上」。
                # 三方实测（_verify/probe_stale_csrf.py）：带对 token 返回
                # code=0；token 错/空返回 CSRF-ERROR；连 skey 都没有才是
                # NOT-LOGINED。它既不是"已登录"，也不是"等一会儿就好"，而
                # 是必须拿**当前** skey 重算 token 才能消掉的状态。
                #
                # 在这个状态存 state，服务端不认：issue #13 实测 08:58:44 判
                # "已登录"、08:58:53 复验就 not-logged；本地 13:15 复现同样结果。
                # 假成功会让用户以为登好了，开抢时又报"登录态已失效"，于是
                # 重扫、再假成功，形成死循环——这正是大家反馈的现象。
                #
                # 所以判据回到唯一可靠的那一条：服务端 get-vip-info 明确返回
                # code=0。csrf 不在这里特殊处理，由 _check_login 内部强制重算
                # token 后重试来消解。
                if has_cookie and not hinted:
                    hinted = True
                    if state == "csrf":
                        c("已检测到扫码，正在建立登录态…请在手机上点【确认登录】。", Fore.YELLOW)
                    else:
                        c("已检测到扫码，但服务端还不认。请在手机上点【确认登录】。", Fore.YELLOW)
                    log(f"有 skey/uin 但服务端返回 {state}，继续等用户在手机上确认")
            except Exception as e:
                log(f"检测异常: {e}")

        if ok:
            # 等 skey 真正下发再存：扫码成功后 cookie 有几秒延迟，
            # 存早了就是一份没有 skey 的空壳，下次还得重扫。
            if not await _wait_login_cookies(ctx, timeout=12):
                log("12 秒内没等到完整登录 cookie（skey/uin）")
            saved, desc = await _save_state(ctx)
            if saved:
                log(f"state 已保存：{desc}")
                # 用存下来的这份 state 重新开一次，服务端认了才算真的登录。
                # 绝不能"存下来就算成功"：issue #13 就是这样假成功的——
                # 打印了"登录成功"，9 秒后复验却是 not-logged。
                #
                # 复验同样走「只打接口、不加载页面」：
                # 一是不用再开窗口（无头也不用起，直接省掉一个浏览器），
                # 二是避免加载页面在登录态有问题时把 skey/uin/tinyid 清掉。
                re_state = None
                try:
                    re_state, _vr, _vn = await verify_state_file()
                except Exception as e:
                    c(f"复验出错: {e}", Fore.YELLOW)

                if re_state == "ok":
                    c(f"登录成功，登录态已保存（{desc}）", Fore.GREEN)
                    log("state 复验通过（重启后可直接沿用）")
                elif re_state == "csrf":
                    c(f"登录态未能确认（csrf 未对齐），已保存 {desc}", Fore.YELLOW)
                    c("  建议再执行一次 [2] 确认；开抢前也会自动复验。", Fore.WHITE)
                    log("state 复验：CSRF-ERROR，未能确认登录")
                    ok = False
                elif re_state is None:
                    c(f"登录态已保存（{desc}），但复验没跑成，未能确认。", Fore.YELLOW)
                    log("state 复验未执行")
                    ok = False
                else:
                    c(f"登录没有成功：服务端仍返回未登录（{re_state}）", Fore.RED)
                    c("  存下来的 cookie 服务端不认，请重新扫码。", Fore.WHITE)
                    c("  扫码后一定要在手机上点【确认登录】。", Fore.WHITE)
                    log(f"state 复验没过: {re_state}")
                    ok = False
            else:
                # 关键：不再假报"已保存"。存不全就明说，并给出可执行的建议。
                c(f"扫码成功，但登录态没能完整保存（{desc}）", Fore.YELLOW)
                c("  原因：腾讯的 skey 是会话 cookie，有时在保存那一刻还没下发完整。", Fore.WHITE)
                c(
                    "  建议：稍等 3~5 秒再执行一次 [2]，让 cookie 下发完整（多数第二次就能存全）。",
                    Fore.WHITE,
                )
                log(f"state 未完整保存：{desc}")
        else:
            c("10分钟超时未登录", Fore.RED)

        if ok:
            c("登录态已处理完毕。按回车关闭这个浏览器窗口，然后回到菜单选 [3] 开抢。", Fore.CYAN)
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass
        return ok
    except Exception as e:
        c(f"扫码登录过程出错: {str(e)[:120]}", Fore.RED)
        log(f"do_login 异常: {e}")
        return False
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


# ---------- 抢购 ----------
async def ensure_login():
    """没登录态就引导扫码。文件在不代表 cookie 还有效，所以要真验一次。

    判定原则（很重要，关系到会不会白让人扫码）：
      只有服务端明确回 NOT-LOGINED 才算失效。
      CSRF-ERROR / unknown / 请求异常 都不作为"失效"依据：前者是 token 对不上
      （skey 其实在，_check_login 内部会 force 重算），中者是没见过的返回码，
      后者只是网络抖了。把它们当失效，就是"明明登录着还被逼着重扫"的根源。
      反过来也绝不判成功——成功只认 code=0，其余一律不报"登好了"。
    """
    if not STATE_PATH.exists():
        c("未找到登录态，首次使用需要先扫码登录", Fore.RED)
        y = input("  现在扫码登录？(y/n): ").strip().lower()
        if y == "y":
            await do_login()
        if STATE_PATH.exists():
            c("已保存登录态（每次操作前都会自动校验有效性）", Fore.YELLOW)
            return True
        c("  未登录，操作已取消", Fore.RED)
        return False

    # 文件在、但里面根本没有 skey/uin：不用再开浏览器白跑 20 秒，
    # 直接说要重扫。这种文件是历史版本"存早了"留下的空壳，必然不可用。
    if _state_missing_skey():
        c(f"保存的登录态不完整（{_state_summary()}）", Fore.RED)
        c("  这份文件里没有 skey，等于没登录。", Fore.WHITE)
        y = input("  现在重新扫码登录？(y/n): ").strip().lower()
        if y == "y":
            return await do_login()
        c("  未登录，操作已取消", Fore.RED)
        return False

    # 只用接口校验，**不加载任何页面**。
    # 加载页面会在登录态失效时把 skey/uin/tinyid 一起清掉（实测 21 条 -> 18 条，
    # 三个全没），等于校验动作本身把登录态毁了，用户只能重扫。
    state, r, cookie_n = await verify_state_file()
    if state == "csrf":
        # CSRF-ERROR = 请求带的 token 与服务端当前 skey 对不上。
        # 这不是「没能确认」，是「确认失败」：以前这里按有效继续，于是后面
        # 每个接口都 CSRF-ERROR，界面只显示「未识别的返回码」，看不出原因。
        # 重算一次 token 再试（skey 可能刚换过），还不行就必须重扫。
        await asyncio.sleep(1.5)
        state, r, cookie_n = await verify_state_file()
    elif state == "unknown":
        # 可能是网络抖动或 EdgeOne 挑战，隔几秒再试一次，
        # 否则会误判失效、让人白扫一次码
        await asyncio.sleep(3)
        state, r, cookie_n = await verify_state_file()
    detail = str(r.get("code") or r.get("__err") or "")[:40]
    log(
        f"登录态校验（只打接口）：{state}"
        f"{' ' + detail if detail else ''}，context {cookie_n} 条 cookie"
    )

    if state == "ok":
        log("登录态有效")
        return True

    if state == "csrf":
        # token 对不上就是不对，别装没事：继续跑只会让每个接口都 CSRF-ERROR
        log(f"登录态 CSRF 校验失败（服务端返回 {detail or 'CSRF-ERROR'}）")
        c("登录态 CSRF 校验失败：token 与服务端当前 skey 对不上。", Fore.RED)
        c("  这种状态继续跑，每个接口都会报 CSRF 错误，请重新扫码一次。", Fore.WHITE)
        y = input("  现在重新扫码登录？(y/n): ").strip().lower()
        if y == "y":
            return await do_login()
        c("  未登录，操作已取消", Fore.RED)
        return False

    if state == "unknown":
        # 无法证明失效 -> 不打扰用户，继续干活；真失效的话开抢前那次复验会报出来
        log(f"登录态未能确认（{state}{': ' + detail if detail else ''}），按有效继续")
        c(f"登录态无法确认（{state}），但也不代表失效，继续尝试。", Fore.YELLOW)
        return True

    c(f"保存的登录态已失效（服务端返回 {detail or 'NOT-LOGINED'}）", Fore.RED)
    y = input("  现在重新扫码登录？(y/n): ").strip().lower()
    if y == "y":
        return await do_login()
    c("  未登录，操作已取消", Fore.RED)
    return False
