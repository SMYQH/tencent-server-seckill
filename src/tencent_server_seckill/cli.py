#!/usr/bin/env python3
"""
腾讯云服务器抢购终极方案 — 浏览器内注入JS直接调API
==================================================
在浏览器上下文中用 fetch() 调用 do-goods API，
浏览器自动处理 cookie、CSRF、所有认证。

比纯 HTTP 快（不需要自己处理认证），比 DOM 点击快（不等面板渲染）。

使用:
  uv run --locked --no-dev tencent-server-seckill-cli               # 抢最近一场
  uv run --locked --no-dev tencent-server-seckill-cli --time 10:00   # 指定时间
  uv run --locked --no-dev tencent-server-seckill-cli --test         # 测试(用服务器专区商品)
"""

import argparse
import asyncio
import time
from datetime import datetime

from .api import qualify_blocked, warmup, warmup_qualify
from .browser import (
    CSRF_HOOK_JS,
    _report_state_cookies,
    async_playwright,
    attach_csrf_sniffer,
    ensure_chromium,
    ensure_csrf,
    launch_browser,
)
from .browser import TimeoutError as PwTimeout
from .products import discover_flash_sale, fetch_price, fmt_price
from .purchase import rapid_purchase
from .settings import (
    ACTIVITY_URL,
    ADVANCE_MS,
    CODE_NO_PERMISSION,
    CODE_SOLD_OUT,
    FLASH_SALE,
    NO_RETRY_CODES,
    QUIET_SECONDS,
    TEST_GOODS,
    TEST_REAL_COST,
    WARMUP_SECONDS,
    session_profile,
)
from .storage import STATE_PATH, log
from .timing import find_nearest_time, sync_time


async def run(target_time=None, test_mode=False):
    pw = await async_playwright().start()
    browser = await launch_browser(pw, headless=False)

    if STATE_PATH.exists():
        ctx = await browser.new_context(
            storage_state=str(STATE_PATH),
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
    else:
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )

    await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    await ctx.add_init_script(CSRF_HOOK_JS)
    page = await ctx.new_page()
    page.set_default_timeout(30000)
    attach_csrf_sniffer(page)

    try:
        # 加载页面
        log("加载活动页...")
        await page.goto(ACTIVITY_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # 检查登录
        if "login" in page.url.lower():
            log("请在浏览器中登录...")
            try:
                await page.wait_for_url("**/console/**", timeout=300000)
                await page.goto(ACTIVITY_URL, wait_until="domcontentloaded")
                await asyncio.sleep(3)
            except PwTimeout:
                pass

        # 保存登录状态（顺手报一下 skey/uin 在不在，不再无条件说"已保存"）
        await ctx.storage_state(path=str(STATE_PATH))
        _names = await _report_state_cookies(ctx)
        log(f"登录状态已保存（cookie {_names}）")

        # 同步时间
        offset_ms = await sync_time(page)

        # 商品：--test 用专区商品，正常模式用自动发现的秒杀商品
        goods = TEST_GOODS if test_mode else FLASH_SALE
        log(f"商品(初始): {goods['name']}")

        # 等待前端 CSRF token 就绪（页面导航期间 evaluate 会报上下文销毁，吞掉重试）
        csrf = None
        for _ in range(50):
            try:
                if await page.evaluate("() => !!window.__latestCsrf"):
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        csrf = await ensure_csrf(page)
        if csrf:
            log(f"CSRF token 已就绪（{len(str(csrf))} 位）")
        else:
            log("未捕获到 CSRF token，请求可能被判 CSRF-ERROR")

        # 现查当前在售场次与商品 id（腾讯换了活动也能自动跟上，不用改代码）
        region_id = goods["goods_param"].get("regionId", 8)
        if not test_mode:
            found, session = await discover_flash_sale(page, region_id)
            if session:
                log(
                    f"活动 {found['activity_id']} | 场次 {session['start']} ~ "
                    f"{session['end']} (id={session['id']})"
                )
            goods = found
            log(
                f"商品: {goods['name']} act_id={goods['act_id']} activity_id={goods['activity_id']}"
            )
            log(f"  价格: {fmt_price(goods.get('price'))}（{goods.get('price_check', '未校验')}）")
        else:
            log(f"测试商品: {goods['name']} act_id={goods['act_id']}")

        if test_mode:
            log("测试下单流程：用 109 元专区商品真下单（preview=0），不付款")
            log("  与 38 元那款不是同一商品，仅同配置；可验证登录/参数/首单资格/建单")
            log("  测完请到控制台取消订单；不要频繁测，反复建单容易触发风控")
            price = await fetch_price(
                page,
                goods["activity_id"],
                goods["act_id"],
                goods["type"],
                goods["goods_param"].get("regionId", 8),
            )
            log(f"  价格: {fmt_price(price)}")
            if price and price.get("real_cost") != TEST_REAL_COST:
                log(
                    f"  注意：这个不是 109 元的测试商品（实际 {fmt_price(price)}），"
                    f"act_id 可能已经变了"
                )
            ok = await rapid_purchase(page, goods, duration=5)
            if ok:
                log("已建单 —— 请去控制台取消订单，不要付款。")
            else:
                log("未成交，看上面的返回码说明。")
            return

        # 等待抢购时间
        if target_time:
            now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
            target_dt = datetime.strptime(f"{now.date()} {target_time}", "%Y-%m-%d %H:%M")
        else:
            target_dt = find_nearest_time(offset_ms)

        # 自动找最近场次时 target_time 是 None，这里反推出是哪个场次，
        # 否则取并发档位匹配不上，会退化成默认档
        target_time = target_dt.strftime("%H:%M")

        now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
        wait_sec = (target_dt - now).total_seconds()
        log(f"目标: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (服务器时间)")
        log(f"等待 {wait_sec:.0f}s ({wait_sec / 60:.1f}min)")
        log(f"提前量 {abs(ADVANCE_MS)}ms，会在目标时间前 {abs(ADVANCE_MS) / 1000:.1f} 秒开始")

        target_ms = target_dt.timestamp() * 1000
        fire_local_ms = target_ms + ADVANCE_MS - offset_ms  # 开始抢的时刻换算成本机时间

        warmed_up = False
        resynced = False
        while True:
            remaining = (target_ms - (time.time() * 1000 + offset_ms)) / 1000

            # 最后 QUIET_SECONDS 秒只等待：对时、刷新、预热都要等网络，
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

            # 剩余 3 分钟时重新对时一次，修正长时间等待的漂移
            if remaining <= 180 and not resynced:
                log("剩余3分钟，重新对时...")
                offset_ms = await sync_time(page)
                fire_local_ms = target_ms + ADVANCE_MS - offset_ms
                resynced = True
                continue  # 用新偏移重算 remaining

            if remaining <= WARMUP_SECONDS and not warmed_up:
                warmed_up = True
                # 先查资格（只读）：不合格就当场中止，别白等一整场。
                # 注意服务端返回的是 1/0 而不是 True/False，判断必须走 qualify_blocked
                q = await warmup_qualify(page, goods)
                if q and (
                    qualify_blocked(q.get("user_available"))
                    or qualify_blocked(q.get("idcard_available"))
                ):
                    log("开抢前资格预检就不通过，本场中止。")
                    return
                probe = await warmup(page, goods)
                if probe.get("code") in NO_RETRY_CODES:
                    if probe.get("code") == CODE_SOLD_OUT:
                        log("开抢前这个商品就已经售罄了，中止本场")
                    elif probe.get("code") == CODE_NO_PERMISSION:
                        log(
                            "开抢前就返回「无权创建实例」，这是账号权限问题，不是场次问题，中止本场。"
                        )
                        log(
                            "  子账号：让主账号去 访问管理 → 用户 → 授权，加上 QcloudLighthouseFullAccess；"
                        )
                        log("          买包年包月还要 QCloudFinanceFullAccess（付款权限）。")
                        log("  主账号：查一下实名认证、有没有欠费、是否被风控限制购买。")
                        log(
                            "  这个码每次请求都会返回，继续抢没有意义 —— 跟并发、对时、网络都无关。"
                        )
                    else:
                        log("开抢前已知资格不满足，中止本场")
                    return
                continue

            if int(remaining) % 60 == 0:
                log(f"  还剩 {remaining / 60:.0f}min")

            await asyncio.sleep(min(1.0, max(0.05, remaining - QUIET_SECONDS)))

        # 执行抢购
        open_in = (target_ms - (time.time() * 1000 + offset_ms)) / 1000
        prof = session_profile(target_time)
        log(
            f"本场档位（{target_time or '非场次'}）："
            f"在途 {prof['cap_start']}→{prof['cap_max']} / 回读 {prof['poll_ms']}ms / "
            f"持续 {prof['duration']}s"
        )
        success = await rapid_purchase(page, goods, open_in=open_in, profile=prof)

        if success:
            log("抢购成功，请在浏览器中完成支付")
            await asyncio.sleep(600)
        else:
            log("未抢到。")

    except KeyboardInterrupt:
        log("用户中断")
    except Exception as e:
        log(f"错误: {e}")
        import traceback

        traceback.print_exc()
    finally:
        try:
            await ctx.storage_state(path=str(STATE_PATH))
        except BaseException:
            pass
        await browser.close()
        await pw.stop()


def main():
    parser = argparse.ArgumentParser(description="腾讯云服务器抢购终极方案")
    parser.add_argument("--time", type=str, help="HH:MM")
    parser.add_argument(
        "--test",
        action="store_true",
        help="测试下单流程：用 109 元专区商品真下单（不付款，测完请及时取消）",
    )
    args = parser.parse_args()

    ensure_chromium()
    asyncio.run(run(target_time=args.time, test_mode=args.test))


if __name__ == "__main__":
    main()
