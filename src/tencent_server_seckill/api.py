import asyncio

from .browser import ensure_csrf
from .settings import (
    API_BASE,
    CHECK_AVAILABLE_PATH,
    CODE_DESC,
    CODE_NOT_STARTED,
    CODE_OK,
    DO_GOODS_URL,
    NO_RETRY_CODES,
    REGION_NAMES,
)
from .storage import log


async def api_post(page, path, body):
    """在页面上下文里 POST act-api：cookie / CSRF / 同源都交给浏览器

    csrf 先由 ensure_csrf 保证拿到；万一仍被判 CSRF-ERROR，重新抓一次再重试一次。
    以前是拿着空 token 直接发，失败就当成"没登录"，把有效登录态误判成失效。
    """
    await ensure_csrf(page)
    r = await _api_post_once(page, path, body)
    if _is_csrf_error(r):
        await asyncio.sleep(0.3)
        await ensure_csrf(page)
        r2 = await _api_post_once(page, path, body)
        if not _is_csrf_error(r2):
            return r2
    return r


def _is_csrf_error(r):
    if not isinstance(r, dict):
        return False
    code = str(r.get("code", "")).strip().upper()
    return "CSRF" in code


async def _api_post_once(page, path, body):
    return await page.evaluate(
        """
        async (p) => {
            try {
                const r = await fetch(p.url, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json',
                              'x-csrf-token': window.__latestCsrf || ''},
                    credentials: 'include',
                    body: JSON.stringify(p.body),
                });
                return await r.json();
            } catch (e) {
                return {code: -1, msg: String((e && e.message) || e)};
            }
        }
    """,
        {"url": API_BASE + path, "body": body},
    )


async def do_goods_raw(page, goods, preview=0, concurrency=1):
    """调 do-goods。preview=1 是预检：服务端完整校验，但不产生订单。
    返回 list（并发时有多条）。"""
    await ensure_csrf(page)
    result = await page.evaluate(
        """
        async (params) => {
            const one = () => fetch(params.url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json',
                          'x-csrf-token': window.__latestCsrf || ''},
                credentials: 'include',
                body: JSON.stringify({
                    activity_id: params.activity_id,
                    goods: [{act_id: params.act_id, type: params.type,
                             goods_param: params.goods_param}],
                    agent_channel: {fromChannel: '', fromSales: '', fromUrl: '',
                                    isAgentClient: false},
                    preview: params.preview,
                }),
            }).then(r => r.json()).catch(e => ({code: -1, msg: String((e && e.message) || e)}));

            const jobs = [];
            for (let i = 0; i < params.concurrency; i++) jobs.push(one());
            return await Promise.all(jobs);
        }
    """,
        {
            "url": DO_GOODS_URL,
            "activity_id": goods["activity_id"],
            "act_id": goods["act_id"],
            "type": goods["type"],
            "goods_param": goods.get("goods_param", {}),
            "preview": int(preview),
            "concurrency": int(concurrency),
        },
    )
    if isinstance(result, dict):
        return [result]
    return result or []


async def probe_goods(page, goods):
    """preview=1 预检：真实走一遍服务端校验，但不产生订单。返回原始返回体。"""
    results = await do_goods_raw(page, goods, preview=1, concurrency=1)
    return results[0] if results else {"code": -1, "msg": "无返回"}


async def check_qualify(page, goods, regions=None):
    """调 check-available 查「我有没有资格买」和「各地域还剩多少」。

    这个接口只查不买，preview 字段对本接口没有实际意义（实测都是只读）。
    请求结构必须严格是下面这样，多一个字段服务端就报 is not allowed：

        {"activity_id": AID,
         "goods": [{"act_id": ACT, "region_id": [4, 8, 1]}],
         "preview": 0}

    goods 里只有 act_id + region_id 数组，**不能**放 type / goods_param，
    region_id / act_id 也**不能**提到顶层。这是 2026-11-10 实测踩过的坑：
    结构写错时返回 400（"goods" is required）或 500（服务异常），
    会被误读成"接口不支持查库存"。

    返回：
        {"ok": True, "user_available": bool, "idcard_available": bool,
         "total_num": int, "quota": {region_id: 可用总数}, "raw": {...}}
        {"ok": False, "error": "..."}
    """
    rids = [int(r) for r in (regions or [goods.get("goods_param", {}).get("regionId", 8)])]
    body = {
        "activity_id": goods["activity_id"],
        "goods": [{"act_id": goods["act_id"], "region_id": rids}],
        "preview": 0,
    }
    try:
        r = await api_post(page, CHECK_AVAILABLE_PATH, body)
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

    if not isinstance(r, dict):
        return {"ok": False, "error": "返回不是 JSON"}
    if str(r.get("code", "")).strip().upper().find("CSRF") >= 0:
        return {"ok": False, "error": "CSRF 未对齐"}
    if r.get("code") not in (0, None) and r.get("code") != CODE_OK:
        return {"ok": False, "error": f"code={r.get('code')} {str(r.get('msg') or '')[:60]}"}

    data = r.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return {"ok": False, "error": "data 不是预期结构"}
    g = data[0]

    quota = {}
    raw_quota = g.get("quota")
    if isinstance(raw_quota, dict):
        for rid, skus in raw_quota.items():
            n = 0
            if isinstance(skus, dict):
                for info in skus.values():
                    if isinstance(info, dict) and "available" in info:
                        try:
                            n += int(info["available"])
                        except Exception:
                            pass
            quota[rid] = n

    return {
        "ok": True,
        "user_available": g.get("user_available"),
        "idcard_available": g.get("idcard_available"),
        "total_num": g.get("total_num"),
        "quota": quota,
        "raw": g,
    }


async def warmup(page, goods):
    """预热连接：提前发一个「预检请求」把 TCP/TLS 建好，到点直接用。
    用 preview=1，不会真的下单。返回服务端返回体，供上层判断是否需要中止。"""
    log("预热连接...")
    result = await probe_goods(page, goods)
    code = result.get("code")
    if code == CODE_OK:
        log("  预热返回成功（说明此刻已在窗口内且资格 OK）")
    elif code in NO_RETRY_CODES:
        log(f"  预热返回 {code}: {CODE_DESC.get(code, '')}")
        log(f"  服务器原话: {str(result.get('msg') or '')[:70]}")
    elif code == CODE_NOT_STARTED:
        log("  预热完成（还没到场次时间，正常）")
    else:
        log(
            f"  预热完成（code={code}，{CODE_DESC.get(code, '未识别的返回码，请附服务器原话发 issue')}）"
        )
        log(f"  服务器原话: {str(result.get('msg') or '')[:70]}")
    return result


def qualify_blocked(v):
    """判断资格字段是否表示"不可买"。

    实测（2026-11-11 场次前，登录态，真实商品）返回的是**数字 1**，不是布尔 True：
        user_available = 1
        idcard_available = 1
        total_num = 1
    所以不能用 `v is False` 判断 —— 服务端真返回 0 时 `0 is False` 在 Python 里是假，
    拦截逻辑会整个失效。这里统一按数值判断，只有明确的"不行"才算拦。
    None / 字段缺失 一律放行：宁可多试一场，也不能因为一个辅助字段误杀用户的抢购。
    """
    if v is None:
        return False
    if isinstance(v, bool):
        return not v
    try:
        return int(v) == 0
    except (TypeError, ValueError):
        return False


async def warmup_qualify(page, goods, regions=None):
    """预热阶段顺带查资格与库存（只读，不会下单）。

    这一步的价值：资格不合格（比如这个实名主体已经买过）时，
    当场就能知道，不用傻等一小时、开抢才发现每条都返 1100106。
    查不通（网络/CSRF/接口变动）只提示，不阻断——不能因为一个辅助检查
    把真正的抢购拦下来。
    """
    q = await check_qualify(page, goods, regions)
    if not q.get("ok"):
        log(f"  资格预检没查通（{q.get('error')}），不影响抢购，继续")
        return None

    ua = q.get("user_available")
    ia = q.get("idcard_available")
    tn = q.get("total_num")
    quota = q.get("quota") or {}

    def _yn(v):
        """1/0、True/False、None 都显示成人话。"""
        if v is None:
            return "未知"
        if isinstance(v, bool):
            return "是" if v else "否"
        try:
            return "是" if int(v) else "否"
        except (TypeError, ValueError):
            return str(v)

    log(f"  资格预检：账号可买={_yn(ua)}  实名可买={_yn(ia)}  每账号限购={tn}")
    if quota:
        parts = []
        for rid in sorted(quota, key=lambda x: str(x)):
            name = REGION_NAMES.get(int(rid), rid) if str(rid).isdigit() else rid
            parts.append(f"{name}={quota[rid]}")
        log(f"  各地域剩余库存：{'  '.join(parts)}")
        if sum(quota.values()) == 0:
            log("  当前显示三个地域都是 0 —— 场次没开时正常（名额还没放出来）")

    if qualify_blocked(ua):
        log("  ★ 账号维度不可买：这个账号已经被判定不符合条件，本场大概率白抢。")
        log("    去活动页看下是不是这个商品已经买过、或账号还有未付款的同款订单。")
    if qualify_blocked(ia):
        log(
            "  ★ 实名维度不可买：同一实名主体已有付费记录（服务器原话「相同实名认证主体"
            "已产生付费，不满足新用户条件」，码 1100106）。"
        )
        log("    换个实名主体才有戏，本场继续抢没有意义。")
    else:
        log("  注意：user_available / idcard_available 都是「是」也不代表新用户资格。")
        log("    2026-11-11 实测：开窗前 1.4 秒 preview=1 才返 1100106，")
        log("    更早两条都是 1101001。这层判定服务端临近开窗才做。")
    return q
