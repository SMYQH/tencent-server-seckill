import asyncio
import time
from datetime import datetime, timedelta

from .api import api_post
from .settings import (
    ADAPT_GAIN_MIN,
    ADAPT_UP,
    CAP_FLOOR,
    ONE_WAY_FALLBACK_MS,
    PRE_FIRE_MAX_MS,
    PRE_FIRE_MIN_MS,
    PRE_FIRE_MULT,
    SECKILL_TIMES,
)
from .storage import log


def calc_pre_fire(rtt_ms):
    """按实测往返算「提前多少毫秒放开全速」。

    抽成独立函数是为了能单测——这段逻辑决定第一批量请求是正点到达、
    还是白打在开窗前，不能靠跑一次抢购来验证。

    传 None 表示还没量到往返，用保守的默认单程。
    """
    one_way = ONE_WAY_FALLBACK_MS if not rtt_ms else float(rtt_ms) / 2
    return min(float(PRE_FIRE_MAX_MS), max(float(PRE_FIRE_MIN_MS), one_way * PRE_FIRE_MULT))


def calc_cap(cap_now, rtt_med, prev, cap_max, cap_floor=None, cap_safe=None):
    """决定在途数下一步该加、该退、还是不动。

    判据是「吞吐有没有跟着在途一起涨」，不是「往返涨没涨」——
    往返涨了但吞吐也在涨，说明服务端在并行处理，那时候该继续加。

        prev = (加之前那次的在途数, 当时的往返)
        吞吐比 = (cap_now ÷ rtt_med) ÷ (prev_cap ÷ prev_rtt)

    抽成纯函数是为了能单测：这段逻辑决定整场能发多少请求，
    不能靠真抢一场来验证（一天只有两场，还不一定抢得到）。

    cap_safe 是「确认没问题的值」（一般传场次起点：那是唯一有成功记录的量）。
    判饱和后回到它，而不是回到 prev —— prev 可能本身就已经饱和了，
    实测就撞到过这种情况：30→45→64，判饱和后退回 45，而 45 其实也在排队。

    返回 (新的在途数, 动作)，动作是：
        "probe"      第一次，没有对照，试加一次
        "up"         吞吐跟着涨了，继续加
        "hold"       已经到本场上限，不加
        "saturated"  吞吐没跟着涨，退回安全值，本场别再试
        "keep"       样本不足，不动
    """
    floor_ = CAP_FLOOR if cap_floor is None else int(cap_floor)
    try:
        cap_now = int(cap_now)
        cap_max = int(cap_max)
    except Exception:
        return floor_, "keep"
    cap_now = max(floor_, min(cap_max, cap_now))

    if not rtt_med:
        return cap_now, "keep"

    # 第一次评估，还没有「加之前」的对照，只能试着加一次
    if prev is None:
        if cap_now >= cap_max:
            return cap_now, "hold"
        return min(cap_max, max(cap_now + 1, int(cap_now * ADAPT_UP))), "probe"

    prev_cap, prev_rtt = prev
    try:
        prev_cap = max(1.0, float(prev_cap))
        prev_rtt = max(1.0, float(prev_rtt or rtt_med))
        rtt_med = max(1.0, float(rtt_med))
    except Exception:
        return cap_now, "keep"

    if prev_cap >= cap_now:
        # 这一轮没加过（本来就顶在上限上），没有「加了之后吞吐变没变」这件事。
        # 不加这个判断，稳态时 gain 会算成 1.0 < 1.1，把自己误判成饱和退回去。
        return cap_now, "hold"

    gain = (cap_now / rtt_med) / (prev_cap / prev_rtt)
    if gain >= ADAPT_GAIN_MIN:
        # 吞吐确实涨了：服务端还有余量
        if cap_now >= cap_max:
            return cap_max, "hold"
        return min(cap_max, max(cap_now + 1, int(cap_now * ADAPT_UP))), "up"
    # 加了在途、吞吐却几乎没动 = 真饱和。
    # 退回**安全值**（场次起点），不是退回 prev：prev 可能就是导致饱和的那个数。
    # 抢购比的是谁先到，把请求挂在长队里等于主动放弃。
    if cap_safe is None:
        safe = floor_
    else:
        safe = max(floor_, min(cap_max, int(cap_safe)))
    return max(floor_, min(int(prev_cap), safe)), "saturated"


def median(xs):
    """中位数。样本量小的时候比平均值抗单条异常（比如一次 GC 卡了 1 秒）。"""
    vals = sorted(float(x) for x in xs if x is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


async def sync_time(page):
    """对时：调 /dianshi/get-now 取服务器毫秒，按往返时延折半校正。

    取「往返最快的那一次」而不是中位数：网络抖动只会让往返变慢、把偏移算偏，
    不会让它变快，所以最快的那次最接近真实值。往返相差不大时取这几组的中位数。
    get-now 直接给毫秒，实测单次往返 130~470ms，校正后偏差在 ±20ms 内。
    旧实现用响应头 Date 对时，那只有秒精度，实测系统性偏小 0.6~0.7 秒，
    会让整场抢购整体推迟同样的量。
    """
    SAMPLES = 7
    samples = []  # (rtt, offset)
    for _ in range(SAMPLES):
        t0 = time.time() * 1000
        try:
            resp = await api_post(page, "/dianshi/get-now", {})
        except Exception:
            resp = {}
        t1 = time.time() * 1000
        srv = resp.get("data")
        if resp.get("code") == 0 and isinstance(srv, (int, float)) and srv > 0:
            samples.append((t1 - t0, srv - (t0 + t1) / 2))
        await asyncio.sleep(0.05)

    if samples:
        best_rtt = min(r for r, _ in samples)
        near = [o for r, o in samples if r <= best_rtt + 20]  # 和最快那次差不多的
        near.sort()
        offset = int(near[len(near) // 2])
        spread = int(max(o for _, o in samples) - min(o for _, o in samples))
        log(
            f"服务器时间偏移: {offset:+d}ms "
            f"(get-now ×{len(samples)}, 最快往返 {best_rtt:.0f}ms, 全部波动 {spread}ms)"
        )
        return offset

    log("get-now 对时失败，退回 Date 响应头（秒精度，误差可达 1 秒）")
    return await _sync_time_by_date(page)


async def _sync_time_by_date(page):
    """兜底对时：采样 act-api 响应头 Date 取中位数（只有秒精度）"""
    import email.utils

    SAMPLES = 7
    offsets = []

    async def one_sample():
        fut = asyncio.get_event_loop().create_future()

        def on_resp(resp):
            if fut.done():
                return
            if "act-api" in resp.url:
                d = resp.headers.get("date") or resp.headers.get("Date")
                if d:
                    try:
                        dt = email.utils.parsedate_to_datetime(d)
                        fut.set_result(int(dt.timestamp() * 1000) - int(time.time() * 1000))
                    except Exception:
                        pass

        page.on("response", on_resp)
        try:
            # 主动触发一次 act-api 查询请求（get-vip-info，无害查询接口）
            await page.evaluate(
                """
                (url) => fetch(url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'x-csrf-token': window.__latestCsrf || ''},
                    credentials: 'include', body: '{}'
                }).then(r => r.json()).catch(e => ({}))
            """,
                "https://act-api.cloud.tencent.com/user/get-vip-info",
            )
            off = await asyncio.wait_for(fut, timeout=3)
            offsets.append(off)
        except Exception:
            pass
        finally:
            page.remove_listener("response", on_resp)

    for _ in range(SAMPLES):
        await one_sample()
        await asyncio.sleep(0.15)

    if offsets:
        offsets.sort()
        offset = int(offsets[len(offsets) // 2])
        log(f"服务器时间偏移: {offset:+d}ms (Date头对时 ×{len(offsets)}中位数 {offsets})")
        return offset

    # 回退：页面 nowTime（静态值，含页面加载延迟，精度较差）
    try:
        offset = await page.evaluate("""
            () => {
                const scripts = document.querySelectorAll('script');
                for (const s of scripts) {
                    const m = (s.textContent||'').match(/"nowTime"\\s*:\\s*(\\d{13})/);
                    if (m) return parseInt(m[1]) - Date.now();
                }
                return 0;
            }
        """)
    except Exception:
        offset = 0
    log(f"服务器时间偏移: {offset:+d}ms (nowTime回退)")
    return offset


def find_nearest_time(offset_ms=0):
    """找最近的抢购场次"""
    now = datetime.fromtimestamp((int(time.time() * 1000) + offset_ms) / 1000)
    for t in SECKILL_TIMES:
        candidate = datetime.strptime(f"{now.date()} {t}", "%Y-%m-%d %H:%M")
        if candidate > now + timedelta(seconds=5):
            return candidate
    tomorrow = now.date() + timedelta(days=1)
    return datetime.strptime(f"{tomorrow} {SECKILL_TIMES[0]}", "%Y-%m-%d %H:%M")
