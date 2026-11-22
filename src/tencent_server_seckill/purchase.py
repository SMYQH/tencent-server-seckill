import asyncio
import time
from importlib.resources import files

from .browser import ensure_csrf
from .settings import (
    ADAPT_MIN_MS,
    CAP_CEIL,
    CAP_FLOOR,
    CODE_ALREADY_BOUGHT,
    CODE_DESC,
    CODE_FACE_VERIFY,
    CODE_NO_QUALIFY,
    CODE_OK,
    CODE_RATE_LIMIT,
    CODE_SOLD_OUT,
    DEFAULT_PROFILE,
    DO_GOODS_URL,
    DURATION_SEC,
    FACE_VERIFY_URL,
    FIRE_TIMEOUT_MS,
    NO_RETRY_CODES,
    POLL_MS,
    PRE_FIRE_MAX_MS,
    PROBE_CAP,
    PROBE_MS,
    QUEUE_CODES,
    RATE_MS,
    RL_HIGH_RATIO,
    RL_MIN_DONE,
    RTT_SAMPLES,
    SLOW_RTT_WARN_MS,
)
from .storage import log
from .timing import calc_cap, calc_pre_fire, median

FIRE_JS = (files("tencent_server_seckill") / "scripts" / "purchase.js").read_text(encoding="utf-8")


async def rapid_purchase(page, goods, duration=None, open_in=None, regions=None, profile=None):
    """不等这一批全回来就发下一批：同时在等的到上限就停手，回来一个补一个。

    每秒能发多少只看回来得多快，跟单条请求慢不慢无关；
    成功和致命码从页面里攒下的返回结果读。

    regions：要轮动的地域列表。各地域是不是独立库存还没探明（quota 数字
    全程是 0，抢完再查也只剩全 0），所以默认把请求摊到所有地域上，
    任一处抢到就停；某地域售罄只摘掉它，全部摘完才判本场结束。
    传 None 时只打 goods 里自带的那个地域，行为与以前完全一致。

    profile：场次档位（poll_ms / duration），由 session_profile() 按场次给出。
    传 None 用默认档。**注意档位里没有并发**——在途数运行中按实测往返自动爬坡。

    open_in：距离场次开窗还有多少秒（None=没有窗口概念，直接全速）。
    开窗之前分两段：先每 PROBE_MS 发一条探针（顺带量出往返时间），
    等算出单程、定好提前量以后才放开全速——提前量 = 实测单程 + 余量，
    让第一批请求正好在开窗那一刻到服务端：太早到是白打的 1101001，
    太晚到名额可能已经没了。
    """
    prof = dict(profile or DEFAULT_PROFILE)
    poll_ms = int(prof.get("poll_ms") or POLL_MS)
    timeout_ms = int(prof.get("timeout_ms") or FIRE_TIMEOUT_MS)
    rate_ms = int(prof.get("rate_ms") or RATE_MS)
    if duration is None:
        duration = float(prof.get("duration") or DURATION_SEC)

    # 在途数的起点和上限都来自场次档位（依据见 SESSION_PROFILES 上方注释）。
    # 用户不需要设：10:00 场起点 35 = 首发 30（成功过的规模）+ 5 条留给探针；
    # 15:00 场固定 55 = 首发 50 + 5 条探针，因为下午场补发失效
    # （窗口 2 秒 < 往返还 1.76~2.5 秒），窗口内请求数 ≈ 首发条数。
    cap_max = int(prof.get("cap_max") or CAP_CEIL)
    cap_max = max(CAP_FLOOR, min(CAP_CEIL, cap_max))
    cap_base = int(prof.get("cap_start") or prof.get("inflight") or CAP_FLOOR)
    cap_base = max(CAP_FLOOR, min(cap_max, cap_base))
    cap_now = cap_base
    cap_used_start = cap_base  # 收尾时打「在途从多少爬到多少」用
    cap_peak = cap_base  # 本场爬到过的最高在途数

    open_at = None if open_in is None else time.monotonic() + open_in
    # 已经在窗口里（比如手动抢一场进行中的）就不用探针，直接全速
    ramped = open_in is None or open_in <= 0
    pre_fire = float(PRE_FIRE_MAX_MS)  # 还没量到往返，先按最保守的值占位
    ramp_at = None if open_at is None else open_at - pre_fire / 1000

    # 基线往返：探针阶段只有一条在飞，那时量到的就是纯传播时间。
    # 一旦放开全速就**锁死不再更新** —— 否则满载时「最快那条」会把基线越拉越低，
    # 阈值跟着变严，最后系统把自己误判成一直在排队，白减并发。
    rtt_base = None
    adapt_prev = None  # (加之前的在途, 当时的往返)，算吞吐比要用
    adapt_saturated = False  # 已判定饱和的场次不再往上试（抢购窗口只有几秒，试错太贵）
    adapt_gap = ADAPT_MIN_MS / 1000.0  # 评估周期，拿到基线后改成「至少一个往返」
    adapt_at = time.monotonic() + adapt_gap
    last_adapt_log = None  # 上一次调整时打的日志，用来把连续同向调整合成一条

    # 开抢前把 csrf 落实：FIRE_JS 里发的是 window.__latestCsrf，
    # 这里为空的话，整场几百条请求会全部被服务端判 CSRF-ERROR，白打一整天。
    if not await ensure_csrf(page):
        log("!! 没拿到 csrf token，本次请求可能全部被判 CSRF-ERROR")

    use_regions = list(regions) if regions else [goods.get("goods_param", {}).get("regionId", 8)]

    if not ramped:
        log(f"距开窗 {open_in:.1f}s，先每 {PROBE_MS}ms 一条探针量往返，开窗前按实测单程提前铺开")
    if len(use_regions) > 1:
        log(
            f"开始抢购：轮动 {len(use_regions)} 个地域 {use_regions}，"
            f"在途 {cap_base} 起、最多 {cap_max}（按实测吞吐自动加），"
            f"限速 {1000 // rate_ms} 条/秒，"
            f"每 {poll_ms}ms 看一次结果，持续 {duration:g}s"
        )
        log("  某个地域售罄只摘掉它，其余继续；全部售罄才停")
    else:
        log(
            f"开始抢购：在途 {cap_base} 起、最多 {cap_max}（按实测吞吐自动加），"
            f"限速 {1000 // rate_ms} 条/秒，"
            f"每 {poll_ms}ms 看一次结果，持续 {duration:g}s"
        )

    params = {
        "url": DO_GOODS_URL,
        "activity_id": goods["activity_id"],
        "act_id": goods["act_id"],
        "type": goods["type"],
        "goods_param": goods.get("goods_param", {}),
        "regions": use_regions,
        "sold_code": CODE_SOLD_OUT,
        "preview": 0,  # 真抢：会真的下单
        "cap": int(cap_now) if ramped else min(PROBE_CAP, int(cap_now)),
        "batch": int(cap_now) if ramped else 1,
        "ok": CODE_OK,
        "rtt_n": int(RTT_SAMPLES),
        "reset_rtt": False,  # 换了在途数就置 True，让页面把往返样本清空重采
        # 售罄不放进 norety：它只摘地域，全部摘完才由 JS 判本场结束
        "norety": sorted(NO_RETRY_CODES - {CODE_SOLD_OUT}),
        "timeout_ms": int(timeout_ms),  # 单条超时，见 FIRE_TIMEOUT_MS 的说明
        "rate_ms": int(rate_ms),  # 令牌桶步长，见 RATE_LIMIT 的说明
        "reset": True,
        "stop": False,
    }

    start = time.monotonic()
    success = False
    last_code = None
    seen_codes = {}
    fired = 0
    err_count = 0
    slow_warned = False  # 网络慢的提示只打一次，别刷屏
    next_progress = start + 2
    result = {}
    done_prev = 0  # 上一轮回来多少条、其中 408 多少条：用来看这一轮是不是整批被打回
    rl_prev = 0
    rl_log_at = 0.0  # 上次打印 408 占比的时刻，用于日志降噪

    while time.monotonic() - start < duration:
        now = time.monotonic()
        if not ramped and open_at is not None:
            # 每轮都按最新量到的往返重算「什么时候铺开」：提前量 = 单程 × 2，
            # 保证开窗瞬间第一批正好压到服务端，且白打的量恒定为一个批次。
            # 算法见 calc_pre_fire 与上方常量注释。
            rtt = result.get("rtt")
            if rtt:
                # 探针阶段只有 1 条在飞，此刻的往返就是纯传播时间，用它当基线。
                # 放开全速后（ramped=True）这段不再执行，基线就锁死在这里。
                rtt_base = float(rtt)
            pre_fire = calc_pre_fire(rtt)
            ramp_at = open_at - pre_fire / 1000
            if rtt and float(rtt) > SLOW_RTT_WARN_MS and not slow_warned:
                # 网络慢的时候，参数怎么调都是次要的 —— 先把话说明白，
                # 免得用户以为是自己并发设错了、一直在我们这边找原因。
                slow_warned = True
                log(f"  !! 实测往返 {rtt}ms，偏慢（常见值 100~300ms）")
                log(
                    f"     单程就要 {float(rtt) / 2:.0f}ms，开窗那一刻我们已经比快网络的人晚这么多。"
                )
                log(
                    "     若走了代理/VPN/加速器，或线路绕了境外，换回直连试试，比调任何参数都管用。"
                )
                log(f"     参数会照常自适应（提前 {pre_fire:.0f}ms 铺开），但速度上补不回来。")
            if now >= ramp_at:
                ramped = True
                params["cap"] = int(cap_now)
                params["batch"] = int(cap_now)
                left = open_at - now
                rtt_txt = f"{rtt}ms" if rtt else "还没量到"
                if left > 0:
                    log(
                        f"  开窗前 {pre_fire:.0f}ms 铺开（限速 {1000 // rate_ms} 条/秒，"
                        f"实测往返 {rtt_txt}，距开窗 {left:.2f}s）"
                    )
                else:
                    log(f"  开窗了，按限速 {1000 // rate_ms} 条/秒铺开（实测往返 {rtt_txt}）")
            else:
                params["cap"] = min(PROBE_CAP, int(cap_now))
                params["batch"] = 1
        params["reset"] = False
        try:
            result = await page.evaluate(FIRE_JS, params) or {}
            params["reset_rtt"] = False  # 清一次就够了，别每轮都清
        except Exception as e:
            # evaluate 可能撞页面导航/关闭抛错，单次失败不中断整个窗口
            err_count += 1
            if err_count <= 3:
                log(f"  发射失败: {str(e)[:80]}")
            await asyncio.sleep(poll_ms / 1000)
            continue

        fired = int(result.get("fired") or 0)
        msgs = result.get("msgs") or {}
        bad_code = None
        for key, cnt in (result.get("codes") or {}).items():
            try:
                code = int(key)
            except Exception:
                code = key  # 登录态失效时返回的是字符串码，别吞掉
            if code == CODE_OK:
                continue  # 成功走下面的 hit 分支，不当作异常码打印
            if code in seen_codes:
                continue
            # 同一种码只报一次，避免刷屏；但一定带上服务器的原话
            seen_codes[code] = cnt
            if isinstance(code, str) and code not in ("undefined", "null"):
                # NOT-LOGINED 之类：接口根本没走到抢购逻辑，继续发没意义
                if "CSRF" in code.upper():
                    desc = "CSRF 校验失败：token 与服务端当前 skey 对不上"
                else:
                    desc = "不是抢购结果码，登录态可能失效了"
            else:
                desc = CODE_DESC.get(code, "未识别的返回码，程序会继续；请附服务器原话发 issue")
            log(f"  返回 {code}: {desc} | {msgs.get(key, '')}")
            if isinstance(code, str) and code not in ("undefined", "null"):
                bad_code = code

        # 「最后返回码」取最新回来那一条，不是随便挑一个（同时有多条在路上时，回来的顺序是乱的）
        if result.get("last") is not None:
            last_code = result["last"]

        hit = result.get("hit")
        if hit:
            deal = hit.get("data") or {}
            log("下单成功")
            log(f"   订单号: {(deal.get('deal_names') or ['?'])[0]}")
            log(f"   大单号: {deal.get('big_deal_no') or '?'}")
            success = True
            break

        fatal = result.get("fatal")
        if fatal is not None:
            if fatal == CODE_SOLD_OUT:
                # 走到这里说明**所有**轮动地域都售罄了（单个地域售罄只会把它摘掉）
                n_sold = int(result.get("sold_count") or 0)
                n_all = int(result.get("region_count") or 1)
                if open_at is None:
                    log(f"  所有地域都已售罄（{n_sold}/{n_all}），本场没有名额了，停止。")
                else:
                    ms_from_open = (time.monotonic() - open_at) * 1000
                    log(
                        f"  所有地域都已售罄（{n_sold}/{n_all}），本场没有名额了，停止。"
                        f"（距开窗 {ms_from_open:+.0f} 毫秒）"
                    )
            elif fatal == CODE_FACE_VERIFY:
                # 账号级拦截：窗口内外都会拦，重试对本场毫无意义，还会加深风控
                log("  ★ 账号需要人脸核身校验，本场不会再成功了，立即停止。")
                log(f"  去这里认证（扫码后通常即时通过）：{FACE_VERIFY_URL}")
                log("  认证完再等下一场；没认证前继续请求只会加重风控标记。")
                try:
                    import webbrowser

                    webbrowser.open(FACE_VERIFY_URL)
                except Exception:
                    pass
            elif fatal in (CODE_ALREADY_BOUGHT, CODE_NO_QUALIFY):
                log(f"  账号这边过不去（{CODE_DESC.get(fatal, '')}），本场停止。")
            else:
                log("  这类错误重试没有意义，本场停止。")
            last_code = fatal
            break

        if bad_code is not None:
            if "CSRF" in str(bad_code).upper():
                log("  本场停止：CSRF 校验失败，token 与服务端当前 skey 对不上，请重新扫码再来。")
            else:
                log("  本场停止，重新扫码登录再来。")
            last_code = bad_code
            break

        now = time.monotonic()
        if now >= next_progress:
            next_progress = now + 2
            rate = fired / max(0.001, now - start)
            log(
                f"  已发 {fired} 条（{rate:.0f}/秒），"
                f"在途上限 {cap_now}，还在等 {int(result.get('inflight') or 0)} 条"
                f" ({now - start:.1f}s)"
            )

        # ---- 在途数自适应：这一整场能发多少条，全靠它 ----
        # 判据是「加了在途，吞吐有没有跟着涨」，不是「往返涨没涨」。详见 calc_cap。
        # 只在放开全速后做：探针阶段是故意只放 1 条的，拿来判断吞吐会得出错误结论。
        if ramped and now >= adapt_at:
            dts = [x for x in (result.get("dts") or []) if x]
            rtt_med = median(dts)
            if rtt_base is None and dts:
                # 没有探针阶段（手动抢一场进行中的）：拿这批里最快的一条当传播时间。
                rtt_base = min(float(x) for x in dts)
            ref_rtt = rtt_med or rtt_base
            if ref_rtt:
                # 评估周期要盖住「换在途后队列刷新」这件事。
                # 换了在途数，队列里排在前面的是改之前发出去的旧请求（它们先回来），
                # 这时候量到的往返还是旧在途的水平。要等这批排空、新请求开始回来，
                # 看到的才是新在途下的真实往返。
                #
                # 用**当前实测往返**算，不是基线：满载往返可能是基线的十倍
                # （11-04 抢到那次基线 94ms、满载 940ms）。按基线算会让评估周期
                # 短到 250ms，那时回来的还是旧请求，一路误判「还有余量」加到顶。
                adapt_gap = max(ADAPT_MIN_MS / 1000.0, float(ref_rtt) / 1000.0 * 1.5)

            # 样本至少要能代表一个在途批次的多数，但采集窗口只留 RTT_SAMPLES 条，
            # 所以取两者的较小值——否则「要 22 条、最多只能有 20 条」会永远等不到。
            enough = min(RTT_SAMPLES, max(3, cap_now // 2))
            if len(dts) < enough:
                # 样本还不够（刚换过在途、刚清空，或者往返特别长），100ms 后再看。
                # 不推后一整个周期，否则 15:00 场那种 2.5 秒往返会白等掉半个窗口。
                adapt_at = now + 0.1
            else:
                adapt_at = now + adapt_gap
                if not adapt_saturated:
                    new_cap, action = calc_cap(
                        cap_base, rtt_med, adapt_prev, cap_max, cap_safe=cap_used_start
                    )
                    if action in ("probe", "up"):
                        # 记下「加之前」的状态，下一轮用它算吞吐比
                        adapt_prev = (cap_base, rtt_med)
                        cap_base = new_cap
                        cap_peak = max(cap_peak, cap_base)
                        cap_now = cap_base
                        params["cap"] = int(cap_now)
                        params["batch"] = int(cap_now)
                        params["reset_rtt"] = True  # 换在途了，往返要重新采
                        if action != last_adapt_log:
                            last_adapt_log = action
                            log(
                                f"  在途 加到 {cap_now}（当前往返 {rtt_med:.0f}ms，"
                                f"上限 {cap_max}；一个往返后看吞吐跟不跟得上）"
                            )
                    elif action == "saturated":
                        # 加了在途、吞吐没跟上 → 服务端饱和，本场不再往上试。
                        adapt_saturated = True
                        # 但**不能无条件立刻降**：如果此刻在飞数已经超过新上限，
                        # 降下去会让 `inflight < cap` 再也不成立，新请求一条都发不出去。
                        # 日志实证（11-12）：
                        #   lzj664270555 10:00 场：cap 52 退到 35 时 inflight 正好 35，
                        #     之后 6 秒只发出 27 条，总速率从 25/秒 掉到 16/秒
                        #   cstcood 那场：inflight 64 退到 30，1 秒后本场就结束了
                        # 所以先维持当前上限，等挂住的请求回落（现在有超时兜底了）再降。
                        cur_inflight = int(result.get("inflight") or 0)
                        if cur_inflight > new_cap:
                            log(
                                f"  在途 暂不退回 {new_cap}：当前还有 {cur_inflight} 条在飞，"
                                f"降下去会发不出新请求，先维持 {cap_now}"
                            )
                        else:
                            cap_base = new_cap
                            cap_now = cap_base
                            params["cap"] = int(cap_now)
                            params["batch"] = int(cap_now)
                            params["reset_rtt"] = True
                            log(
                                f"  在途 退回 {cap_now}：加了在途吞吐没跟着涨"
                                f"（往返 {rtt_med:.0f}ms），服务端已饱和，本场不再往上试"
                            )
                    elif action == "hold" and last_adapt_log != "hold":
                        last_adapt_log = "hold"
                        log(f"  在途停在 {cap_now}（已到本场上限）")

        # 408 只记录，**不缩量**。
        # 历史实测：408 是网关按比例随机丢弃，不是封禁；降低并发从未降低 408 率
        # （12 并发反而 76~81%，比 30 并发还高），掉下去的只是总请求量。
        # 11-04 抢到那次被 408 挡掉 109 条（76%），一条没缩，照样成功。
        # 「该不该减在途」现在交给上面的往返判据：服务端真被压垮时往返会先变长，
        # 那是比 408 比例更本源的信号。两个机制同时改 cap 只会互相打架
        # （这一条是实测出来的：两个都在跑时，自适应爬到 64 又被 408 折扣打回 35）。
        codes_now = result.get("codes") or {}
        rl_now = int(codes_now.get(str(CODE_RATE_LIMIT)) or codes_now.get(CODE_RATE_LIMIT) or 0)
        done_now = int(result.get("done") or 0)
        round_done = done_now - done_prev
        round_rl = rl_now - rl_prev
        done_prev, rl_prev = done_now, rl_now
        if round_done >= RL_MIN_DONE and round_rl / round_done >= RL_HIGH_RATIO:
            if now - rl_log_at >= 3.0:  # 每 3 秒最多提一次，不刷屏
                rl_log_at = now
                log(
                    f"  408 占 {round_rl / round_done * 100:.0f}%"
                    f"（按比例丢弃，继续发；要减在途会由往返变长自动触发）"
                )

        if ramped or ramp_at is None:
            await asyncio.sleep(poll_ms / 1000)
        else:
            # 探针阶段不用密，但别睡过该铺开的时刻
            await asyncio.sleep(min(PROBE_MS / 1000, max(0.005, ramp_at - time.monotonic())))

    # 收尾：先让页面停发，再把还没回来的等一会儿，
    # 免得最后半秒的成功返回被丢掉（数量由服务端按账号限 1，不会因为多发重复下单）
    try:
        params["stop"] = True
        deadline = time.monotonic() + 2.5
        while True:
            result = await page.evaluate(FIRE_JS, params) or {}
            if result.get("hit") and not success:
                deal = result["hit"].get("data") or {}
                log("下单成功")
                log(f"   订单号: {(deal.get('deal_names') or ['?'])[0]}")
                log(f"   大单号: {(deal.get('big_deal_no') or '?')}")
                success = True
            if success or int(result.get("inflight") or 0) == 0 or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.1)
        fired = int(result.get("fired") or fired)
    except Exception:
        pass

    elapsed = time.monotonic() - start
    log(f"抢购结束: {fired} 次请求, {elapsed:.1f}s, {'成功' if success else '未抢到'}")
    cap_txt = f"在途 {cap_used_start}→{cap_peak}"
    if rtt_base:
        cap_txt += f"，基线往返 {rtt_base:.0f}ms"
    log(f"  本场自适应：{cap_txt}；平均 {fired / max(0.001, elapsed):.0f} 条/秒")
    tally = result.get("codes") or {}
    msgs = result.get("msgs") or {}
    if tally:

        def _label(k):
            try:
                k = int(k)  # 页面里回来的字典键是字符串
            except Exception:
                pass
            if k == CODE_OK:
                return "成功"
            if k == -1:
                return "没发出去"
            if k == -2:
                return "超时放弃"
            return str(k)

        parts = sorted(tally.items(), key=lambda kv: -kv[1])
        pieces = []
        for k, v in parts:
            # 每个码都带上服务器原话：认不出的码（比如 1101408）靠这句话才能定性
            msg = str(msgs.get(k) or msgs.get(str(k)) or "").strip()
            pieces.append(f"{_label(k)}×{v}" + (f"（{msg}）" if msg else ""))
        log("  各码次数: " + ", ".join(pieces))

        # 超时是客户端单方面放弃，服务端可能还在处理 —— 极端情况下会留下
        # 「幽灵订单」：我们没收到成功回包，但服务端其实建了单。
        # 所以只要本场有超时，就必须提醒去核对，不能默认"没抢到"。
        def _cnt(*keys):
            return sum(int(tally.get(k) or 0) for k in keys)

        n_timeout = _cnt(-2, "-2")
        if n_timeout > 0 and not success:
            log(f"  !! 本场有 {n_timeout} 条请求超时后被放弃。")
            log("     超时只是客户端不等了，服务端可能仍在处理。")
            log("     建议去控制台订单页核对一眼，有没有多出来的未支付订单，有就取消。")

    # 分地域战况：轮动模式下最该看的就是这个——
    # 哪个地域先售罄、哪个地域一条都没发出去、额度是不是被摊平了
    reg_stats = result.get("regions") or {}
    if len(reg_stats) > 1:
        NAME = {4: "上海", 8: "北京", 1: "广州", 9: "新加坡", 999: "非法"}
        bits = []
        for rid, s in sorted(reg_stats.items(), key=lambda kv: str(kv[0])):
            if not isinstance(s, dict):
                continue
            nm = NAME.get(rid, str(rid))
            flag = "已售罄" if s.get("sold") else "仍在打"
            top = ""
            codes = s.get("codes") or {}
            if codes:
                k = max(codes.items(), key=lambda kv: kv[1])[0]
                try:
                    k = int(k)
                except Exception:
                    pass
                top = f" 最多码={k}"
            bits.append(f"{nm} 发{s.get('sent', 0)}回{s.get('done', 0)}({flag}{top})")
        log("  各地域: " + " | ".join(bits))

        # 库存结构判定：只看窗口内各地域「第一次收到售罄回包」的时间。
        # 抢完再去查 quota 是没用的 —— 那时名额早光了，三地必然都是 0。
        # 注意：这是客户端收到回包的时间，不是服务端扣减库存的时刻；
        # 三地请求发起有先后、回包也有排队抖动，所以下面只给「偏向」，不下定论。
        def _nm(rid):
            try:
                return NAME.get(int(rid), str(rid))
            except (TypeError, ValueError):
                return str(rid)

        sold_at = {}
        still_alive = []
        for rid, s in reg_stats.items():
            if not isinstance(s, dict):
                continue
            t = s.get("sold_at")
            if t:
                try:
                    sold_at[_nm(rid)] = float(t)
                except (TypeError, ValueError):
                    pass
            elif not s.get("sold") and (s.get("sent") or 0) > 0:
                still_alive.append(_nm(rid))

        if sold_at and still_alive:
            log(
                f"  库存判定：{'、'.join(sold_at)}已售罄，"
                f"{'、'.join(still_alive)}仍在打 → 偏向库存按地域分开"
            )
            log("    → 若确实分开，该把请求压到还有货的地域，而不是三点均摊。")
        elif len(sold_at) >= 2:
            spread = max(sold_at.values()) - min(sold_at.values())
            who = sorted(sold_at, key=lambda k: sold_at[k])
            if spread <= 300:
                log(
                    f"  库存判定：{'、'.join(who)} 几乎同时售罄（相差 {spread:.0f}ms）"
                    f" → 偏向一个总池，地域只决定机器开在哪。"
                )
            else:
                log(
                    f"  库存判定：{'、'.join(who)} 售罄先后错开 {spread:.0f}ms"
                    f" → 偏向库存按地域分开。"
                )
        else:
            log("  库存判定：本场没有地域返售罄，这条线索没取到（没售罄、或只打单地域）。")
        # 时基限制写进日志，免得有人把「偏向」当实测结论拿去改策略
        if sold_at:
            log("    （比的是客户端收到售罄回包的时间，不是服务端扣减时刻，只作参考）")
    if not success and last_code is not None:
        if isinstance(last_code, str):
            log(f"  最后返回码 {last_code}: 登录态校验没过，重新扫码登录再试")
        elif last_code == CODE_SOLD_OUT:
            log(f"  最后返回码 {last_code}: {CODE_DESC[last_code]}，只能等下一场")
        elif CODE_DESC.get(last_code):
            log(f"  最后返回码 {last_code}: {CODE_DESC[last_code]}")
            if last_code == CODE_FACE_VERIFY:
                log(f"  → 先去做人脸核身：{FACE_VERIFY_URL}，认证完再抢下一场")
        elif last_code == -1:
            log("  最后返回码 -1: 请求本身没发出去（网络/页面上下文问题）")
        elif last_code in QUEUE_CODES:
            log(
                f"  最后返回码 {last_code}: {CODE_DESC.get(last_code, '排队信号')}"
                f"。这类码刷屏不影响抢购，没抢到就是名额没了或时间窗不对"
            )
        else:
            log(f"  最后返回码 {last_code}: 未识别的返回码，请附服务器原话发 issue")
    return success
