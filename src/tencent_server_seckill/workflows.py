import asyncio
import json
import sys
import time
from datetime import datetime

# 复用核心逻辑
from .api import _is_csrf_error, qualify_blocked, warmup, warmup_qualify
from .browser import (
    CSRF_HOOK_JS,
    UA,
    apply_stealth,
    async_playwright,
    attach_csrf_sniffer,
    ensure_csrf,
    launch_browser,
    new_api_context,
)
from .console import HB_WIDTH, Fore, c, log
from .login import (
    _check_login,
    _save_state,
    _wait_csrf,
    csrf_from_context,
    do_login,
    ensure_login,
    verify_state,
)
from .products import discover_flash_sale, fmt_price
from .purchase import rapid_purchase
from .settings import (
    ACTIVITY_URL,
    ADVANCE_MS,
    API_BASE,
    CODE_BUSY,
    CODE_FACE_VERIFY,
    CODE_NOT_STARTED,
    CODE_OK,
    CODE_RATE_LIMIT,
    CODE_SOLD_OUT,
    DO_GOODS_URL,
    FLASH_SALE,
    NO_RETRY_CODES,
    QUIET_SECONDS,
    REGION_NAMES,
    TEST_GOODS,
    TEST_REAL_COST,
    WARMUP_SECONDS,
    session_profile,
)
from .storage import STATE_PATH
from .timing import find_nearest_time, sync_time


async def run_grab(region_id, target_time=None, rotate=True):
    """headless 抢购，顺便用心跳显示浏览器还活着没

    rotate=True 时三个地域轮着发。这里刻意不下「库存按地域独立」的结论——
    quota 虽然以 region_id 为键返回，但数字含义还没探明，且抢完再查只剩全 0。
    轮动是保守做法：两种情况都不吃亏。库存结构的线索只在窗口内取，见
    purchase.py 收尾的「库存判定」。
    """
    if not await ensure_login():
        return False
    goods = {
        **FLASH_SALE,
        "goods_param": {**FLASH_SALE["goods_param"], "regionId": region_id},
    }

    # 本机可下单的地域（不含海外/非法值），轮动时全用上
    grab_regions = [4, 8, 1] if rotate else [region_id]

    c("=" * 56, Fore.YELLOW)
    if rotate:
        c(
            f"开始抢购 | 三地轮动并发：{'/'.join(REGION_NAMES[r] for r in grab_regions)} | 商品: {goods['name']}",
            Fore.YELLOW,
        )
        c("某个地域售罄就不再去它那里，三个都售罄才停", Fore.WHITE)
    else:
        c(
            f"开始抢购 | 地域: {REGION_NAMES.get(region_id, region_id)} | 商品: {goods['name']}",
            Fore.YELLOW,
        )
    c("浏览器已隐藏运行（headless）", Fore.CYAN)
    c("=" * 56, Fore.YELLOW)

    pw = await async_playwright().start()
    browser = await launch_browser(pw, headless=True)

    # 心跳：跑抢购要等很久，给个提示浏览器还活着
    stop_hb = {"v": False}

    def heartbeat():
        while not stop_hb["v"]:
            try:
                alive = browser.is_connected()
            except Exception:
                alive = False
            sys.stdout.write(
                ("\r浏览器状态: %s | Ctrl+C 停止" % ("运行中" if alive else "已关闭")).ljust(
                    HB_WIDTH
                )
            )
            sys.stdout.flush()
            time.sleep(5)

    import threading

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()

    try:
        ctx = await browser.new_context(
            storage_state=str(STATE_PATH), viewport={"width": 1920, "height": 1080}, user_agent=UA
        )
        # 反检测交给 playwright-stealth 整套（navigator.webdriver 只是其中一条）
        await apply_stealth(ctx)
        await ctx.add_init_script(CSRF_HOOK_JS)
        page = await ctx.new_page()
        page.set_default_timeout(30000)
        attach_csrf_sniffer(page)

        log("加载活动页...")
        await page.goto(ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        try:
            state, r = await _check_login(page)
            if state == "not-logged":
                c(f"登录态无效（{r.get('code') or r.get('__err')}）", Fore.RED)
                y = input("  是否现在重新扫码登录？(y/n): ").strip().lower()
                if y == "y":
                    if await do_login():
                        c("  重新登录成功，请回到菜单重新选择场次开始抢购。", Fore.GREEN)
                    else:
                        c("  重新登录失败", Fore.RED)
                return False
            if state == "ok":
                log("登录态有效")
            elif state == "csrf":
                # CSRF-ERROR = token 与服务端当前 skey 对不上。
                # 以前这里"按有效继续"，于是后面每个接口都 CSRF-ERROR，
                # 界面只显示"未识别的返回码"，看不出真正原因。
                c("登录态 CSRF 校验失败（token 与服务端当前 skey 对不上）。", Fore.RED)
                c("  这个状态继续跑只会每个接口都报错，请重新扫码。", Fore.WHITE)
                log(f"登录检查失败（csrf）：{r.get('code') or r.get('__err')}")
                return False
            else:
                log(f"登录态未能确认（{state}），按有效继续；真失效会在开抢前复验时报出来")
                c(f"登录态无法确认（{state}），继续尝试。", Fore.YELLOW)
        except Exception as e:
            c(f"登录检查失败: {e}", Fore.RED)
            return False

        _ok, _desc = await _save_state(ctx)
        log(f"登录态刷新（{_desc}）")

        if not await _wait_csrf(page):
            c("未捕获到 CSRF token，请求可能被拒绝", Fore.YELLOW)

        # 商品 id 现查：腾讯换活动就换 act_id，写死在代码里必然过期
        goods_box = {"goods": goods}

        async def refresh_goods():
            found, session = await discover_flash_sale(page, region_id)
            if session:
                log(
                    f"活动 {found['activity_id']} | 场次 {session['start']} ~ "
                    f"{session['end']} (id={session['id']})"
                )
            goods_box["goods"] = found
            log(
                f"商品: {found['name']} act_id={found['act_id']} activity_id={found['activity_id']}"
            )
            log(f"  价格: {fmt_price(found.get('price'))}（{found.get('price_check', '未校验')}）")

        offset_ms = await sync_time(page)
        await refresh_goods()

        if target_time:
            now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
            target_dt = datetime.strptime(f"{now.date()} {target_time}", "%Y-%m-%d %H:%M")
        else:
            target_dt = find_nearest_time(offset_ms)

        # 自动找最近场次时 target_time 还是 None，这里从算出的时刻反推出是哪个场，
        # 否则取并发档位时匹配不上，就退化成默认档了
        target_time = target_dt.strftime("%H:%M")

        now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
        wait_sec = (target_dt - now).total_seconds()
        log(f"目标: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (服务器时间)")
        log(f"等待 {wait_sec:.0f}s ({wait_sec / 60:.1f}min)")
        log(f"提前量 {abs(ADVANCE_MS)}ms")

        target_ms = target_dt.timestamp() * 1000
        fire_local_ms = target_ms + ADVANCE_MS - offset_ms  # 开始抢的时刻换算成本机时间

        warmed_up = False
        resynced = False
        while True:
            if not browser.is_connected():
                c("\n浏览器已被关闭，程序中止。", Fore.RED)
                return False
            remaining = (target_ms - (time.time() * 1000 + offset_ms)) / 1000

            # 最后 QUIET_SECONDS 秒只等待：对时、刷新页面、预热都要等网络，
            # 压在最后几秒会把提前量吃掉，所以这段时间一次网络请求都不发。
            if remaining <= QUIET_SECONDS:
                log(f"最后 {remaining:.1f}s，只等待不发请求")
                gap = fire_local_ms - time.time() * 1000
                if gap > 0:
                    await asyncio.sleep(gap / 1000)
                left = target_ms - (time.time() * 1000 + offset_ms)
                log(
                    f"实际提前 {left:.0f}ms（设定 {abs(ADVANCE_MS)}ms，"
                    f"偏差 {left - abs(ADVANCE_MS):+.0f}ms）"
                )
                break

            # 剩余 3 分钟：刷新页面 + 复验登录 + 重新对时。
            # 挂机数小时的会话，CSRF token / 登录态都可能过期，
            # 开抢前必须让页面重新生成 token，并确认登录仍然有效。
            # 这一步必须在进纯等待之前做完，来不及就跳过刷新，不占提前量。
            if remaining <= 180 and not resynced:
                if remaining > WARMUP_SECONDS:
                    log("剩余3分钟，刷新页面并重新对时...")
                    csrf_ok = False
                    for attempt in (1, 2):
                        try:
                            if attempt == 1:
                                await page.reload(wait_until="domcontentloaded", timeout=30000)
                            else:
                                await page.goto(
                                    ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000
                                )
                            await asyncio.sleep(2)
                            csrf_ok = await _wait_csrf(page, tries=50)
                            if csrf_ok:
                                break
                            log(f"  第{attempt}次刷新后未捕获到 CSRF，重试...")
                        except Exception as e:
                            log(f"  第{attempt}次刷新失败: {str(e)[:80]}")
                    if csrf_ok:
                        try:
                            # 登录判定走接口而不是页面：页面刚 reload 过，
                            # 它自己的 JS 状态、hook 时机都还没稳，拿它判登录
                            # 容易误报；接口只认 code，不受页面状态干扰。
                            state, r = await verify_state(ctx)
                            if state == "ok":
                                log("登录态复验通过")
                            elif state == "not-logged":
                                c(
                                    f"!! 登录态已失效（{r.get('code') or r.get('__err')}），请中断后重新扫码",
                                    Fore.RED,
                                )
                            elif state == "csrf":
                                # token 与服务端当前 skey 对不上：按当前 skey 重算一次
                                # 页面 token 再复验，还不行就说明这场的请求全会
                                # CSRF-ERROR，不如直接停。
                                log(
                                    f"登录复验 csrf（{r.get('code')}），按当前 skey 重算 token 再试"
                                )
                                await ensure_csrf(page, force=True)
                                await asyncio.sleep(0.5)
                                state2, r2 = await verify_state(ctx)
                                if state2 == "ok":
                                    log("重算 token 后登录态复验通过")
                                elif state2 == "csrf":
                                    c(
                                        "!! 重算 token 后仍是 CSRF 校验失败，本场中止（"
                                        "每个请求都会被服务端拒掉）",
                                        Fore.RED,
                                    )
                                    stop_hb["v"] = True
                                    return False
                                else:
                                    log(f"重算 token 后登录复验状态：{state2}")
                            else:
                                log(f"登录复验未能确认（{state}），继续（不代表登录失效）")
                        except Exception as e:
                            log(f"登录复验异常: {str(e)[:80]}")
                    else:
                        c("!! 刷新后仍未捕获到 CSRF token，开抢可能失败，请留意", Fore.RED)
                else:
                    log("临近开抢，跳过页面刷新，只重新对时...")
                offset_ms = await sync_time(page)
                fire_local_ms = target_ms + ADVANCE_MS - offset_ms
                try:
                    await refresh_goods()
                except Exception as e:
                    log(f"重新拉取场次失败（沿用上次结果）: {str(e)[:80]}")
                resynced = True
                continue

            if remaining <= WARMUP_SECONDS and not warmed_up:
                warmed_up = True
                # 先查资格（只读）：不合格就当场中止，别白等一整场。
                # 注意服务端返回的是 1/0 而不是 True/False，判断必须走 qualify_blocked
                q = await warmup_qualify(page, goods_box["goods"], grab_regions)
                if q and (
                    qualify_blocked(q.get("user_available"))
                    or qualify_blocked(q.get("idcard_available"))
                ):
                    stop_hb["v"] = True
                    c("\n开抢前资格预检不通过，本场中止。", Fore.RED)
                    return False
                probe = await warmup(page, goods_box["goods"])
                if probe.get("code") in NO_RETRY_CODES:
                    stop_hb["v"] = True
                    if probe.get("code") == CODE_SOLD_OUT:
                        c("\n开抢前这个商品就已经售罄了，中止本场。", Fore.RED)
                    else:
                        c("\n开抢前已知资格不满足，中止本场。", Fore.RED)
                    return False
                continue

            if int(remaining) % 60 == 0:
                log(f"  还剩 {remaining / 60:.0f}min")

            await asyncio.sleep(min(1.0, max(0.05, remaining - QUIET_SECONDS)))

        open_in = (target_ms - (time.time() * 1000 + offset_ms)) / 1000
        # 按场次取档位：10:00 场和 15:00 场的拥挤程度不一样（见 settings.py 注释）。
        # 档位里只有回读间隔和持续时间，并发由程序按实测往返自动爬坡。
        prof = session_profile(target_time)
        log(
            f"本场档位（{target_time or '非场次'}）：在途 {prof['cap_start']}→{prof['cap_max']} / "
            f"回读 {prof['poll_ms']}ms / 持续 {prof['duration']}s"
        )
        success = await rapid_purchase(
            page, goods_box["goods"], open_in=open_in, regions=grab_regions, profile=prof
        )
        stop_hb["v"] = True
        if success:
            c("\n" + "=" * 40, Fore.GREEN)
            c("抢购成功，请在腾讯云控制台付款（1小时内）", Fore.GREEN)
            c("=" * 40, Fore.GREEN)
        else:
            c("\n未抢到，本场结束。", Fore.RED)
        return success
    except KeyboardInterrupt:
        c("\n用户中断", Fore.YELLOW)
    except Exception as e:
        c(f"\n错误: {e}", Fore.RED)
        import traceback

        traceback.print_exc()
    finally:
        stop_hb["v"] = True
        try:
            _ok, _desc = await _save_state(ctx)
            log(f"退出前刷新登录态（{_desc}）")
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass
        await pw.stop()
    return False


async def place_single_order(ctx, goods, preview=0, timeout=20):
    """只发**一条** do-goods，不打开任何页面。返回 (code, msg, raw)。

    实测（2026-11-12）：用 ctx.request.post 直接打 act-api，带 g_tk(skey) 算的
    csrf 返回 200 正常业务码，不带则 400 CSRF-ERROR —— 说明请求完全走通，
    服务端不校验 Referer/Origin，也就不需要为了一次试单去加载活动页。

    这一点很重要：加载活动页会跑抢购那种量级的 JS，而且登录态有问题时还会
    把 skey/uin/tinyid 清掉。试单只需要一个请求，不该付这些代价。
    """
    gtk = await csrf_from_context(ctx)
    if not gtk:
        return None, "context 里没有 skey，无法下单", {}
    body = {
        "activity_id": int(goods["activity_id"]),
        "goods": [
            {
                "act_id": int(goods["act_id"]),
                "type": goods["type"],
                "goods_param": goods.get("goods_param", {}),
            }
        ],
        "agent_channel": {
            "fromChannel": "",
            "fromSales": "",
            "fromUrl": "",
            "isAgentClient": False,
        },
        "preview": int(preview),
    }
    try:
        resp = await ctx.request.post(
            DO_GOODS_URL,
            data=json.dumps(body),
            headers={"Content-Type": "application/json", "x-csrf-token": gtk},
            timeout=int(timeout * 1000),
        )
        r = await resp.json()
    except Exception as e:
        return None, f"请求失败: {str(e)[:100]}", {}
    return r.get("code"), r.get("msg") or "", r


# do-goods 的返回码怎么念给人听。只列实测见过的，其余显示原码让用户带原话反馈。
GOODS_CODE_DESC = {
    CODE_OK: "下单成功（已建单）",
    "CSRF-ERROR": "CSRF 校验失败",
    CODE_NOT_STARTED: "秒杀尚未开始（这个商品受时间窗限制）",
    CODE_RATE_LIMIT: "访问频率过快：账号/IP 正在限流，减并发也没用，等 1~2 小时",
    CODE_BUSY: "系统繁忙：服务端在排队",
    CODE_SOLD_OUT: "本期已售罄",
    CODE_FACE_VERIFY: "账号需要人脸核身，去认证后才能买",
    1100106: "检测到相同实名主体已购买该商品，不满足首单条件（这是资格问题，不是程序问题）",
}


async def test_once(region_id):
    """测试下单：用接口单发一条 do-goods，不加载页面、不跑抢购机器。

    【为什么改成这样】
    28de579 曾经把它改成 rapid_purchase(duration=3)，等于把整台抢购机器
    （在途 30→64、每 15ms 回读）直接用来"测一下"。实测有人 3.7 秒打出 360 条
    真下单请求、100% 是 408，账号被打进风控，之后真抢更难。
    试单的目的只是"确认能不能走通、能不能建单"，一条请求足够。
    """
    if not await ensure_login():
        return
    c("!" * 58, Fore.YELLOW)
    c("  测试下单：用 109 元专区商品真下单（preview=0），不付款。", Fore.WHITE)
    c("  **只发 1 条请求，不加载页面、不跑抢购那套并发**，不会因此被限流。", Fore.GREEN)
    c("  能确认：登录态、参数结构、首单资格、能不能建单。", Fore.WHITE)
    c("  不付款的订单 1 小时后自动关闭。抢之前去订单页取消掉。", Fore.YELLOW)
    c("!" * 58, Fore.YELLOW)
    try:
        # 大小写都认：曾有人输 yes 被当成取消，以为程序坏了（issue #14）
        y = input("  确认真下单？输入 YES 继续: ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        return
    if y != "YES":
        c("  已取消", Fore.YELLOW)
        return

    goods = {
        **TEST_GOODS,
        "goods_param": {**TEST_GOODS["goods_param"], "regionId": region_id},
    }
    c(f"  测试商品: {goods['name']} act_id={goods['act_id']}", Fore.CYAN)
    c(f"  地域: {REGION_NAMES.get(region_id, region_id)}", Fore.CYAN)

    pw = None
    browser = None
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await launch_browser(pw, headless=True)
        # 注意：只建 context，**不 new_page、不 goto**，所以没有任何页面和窗口
        ctx = await new_api_context(browser, STATE_PATH)

        # 先用接口确认登录态（不碰页面）
        state, vr = await verify_state(ctx)
        if state == "not-logged":
            c(f"  登录态无效（{vr.get('code') or vr.get('__err')}），先扫码登录再试。", Fore.RED)
            return
        if state == "csrf":
            log(f"试单已取消：登录态 CSRF 校验失败（code={vr.get('code')}）")
            return
        if state != "ok":
            c(f"  登录态未能确认（{state}），继续尝试这一条。", Fore.YELLOW)

        # 价格先查（只读接口，不产生任何写操作）
        try:
            gtk = await csrf_from_context(ctx)
            pr = await ctx.request.post(
                API_BASE + "/dianshi/query-price",
                data=json.dumps(
                    {
                        "activity_id": int(goods["activity_id"]),
                        "goods": [
                            {
                                "act_id": int(goods["act_id"]),
                                "type": goods["type"],
                                "goods_param": {**goods["goods_param"]},
                            }
                        ],
                        "preview": 0,
                    }
                ),
                headers={"Content-Type": "application/json", "x-csrf-token": gtk or ""},
                timeout=15000,
            )
            pj = await pr.json()
            if _is_csrf_error(pj):
                log(f"试单已取消：价格查询 CSRF 校验失败（code={pj.get('code')}）")
                c(
                    f"  价格查询被 CSRF 挡下（{pj.get('code')}）：token 与服务端当前 skey 对不上。",
                    Fore.RED,
                )
                c("  这种状态下单也会被拒，先重新扫码再来。", Fore.WHITE)
                return
            if pj.get("code") != CODE_OK:
                c(
                    f"  价格查询返回 code={pj.get('code')}：{str(pj.get('msg') or '')[:60]}",
                    Fore.YELLOW,
                )
            items = (pj.get("data") if isinstance(pj.get("data"), list) else None) or []
            price = next(
                (x for x in items if int(x.get("act_id") or 0) == int(goods["act_id"])), None
            )
            c(f"  价格: {fmt_price(price)}", Fore.CYAN)
            if price is None:
                # 「价格未知」得说清为什么：接口整个没通，还是里面没有这一项
                c(
                    f"  价格未知：返回里没有 act_id={goods['act_id']} 这一项（共 {len(items)} 项）。",
                    Fore.YELLOW,
                )
            elif price.get("real_cost") != TEST_REAL_COST:
                c(
                    f"  注意：不是 109 元那款（实际 {fmt_price(price)}），act_id 可能已变",
                    Fore.RED,
                )
        except Exception as e:
            c(f"  价格查询失败（不影响下单验证）: {str(e)[:60]}", Fore.YELLOW)

        c("  发送 1 条下单请求…", Fore.CYAN)
        code, msg, raw = await place_single_order(ctx, goods, preview=0)
        if code is None:
            c(f"  请求没发出去：{msg}", Fore.RED)
            log(f"试单请求失败: {msg}")
            return

        desc = GOODS_CODE_DESC.get(code, "未识别的返回码，请把服务器原话发 issue")
        if code == CODE_OK:
            c(f"  ★ 下单成功（code={code}）：{desc}", Fore.GREEN)
            c("  请去控制台把这个订单取消掉，不要付款。", Fore.GREEN)
            deal = raw.get("data") or {}
            if isinstance(deal, dict) and deal.get("deal_names"):
                c(f"  订单号: {deal['deal_names'][0]}", Fore.GREEN)
        else:
            c(f"  返回 code={code}：{desc}", Fore.YELLOW)
            if msg:
                c(f"  服务器原话: {msg}", Fore.WHITE)
        log(f"试单（单发 1 条）: code={code} msg={str(msg)[:80]}")
    except Exception as e:
        log(f"测试下单异常: {str(e)[:120]}")
        return
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
