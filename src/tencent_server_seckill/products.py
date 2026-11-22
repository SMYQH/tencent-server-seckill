import time
from datetime import datetime

from .api import api_post
from .settings import CANDIDATE_ACTIVITY_IDS, CODE_OK, EXPECT_REAL_COST, FLASH_SALE, GOODS_PARAM
from .storage import log


def fmt_price(price):
    """把 query-price 的返回转成人话，price 为空表示没查到"""
    if not price:
        return "价格未知"
    real = price.get("real_cost")
    total = price.get("total_cost")
    if real is None:
        return "价格未知"
    txt = f"活动价 {real / 100:.2f} 元"
    if total:
        txt += f" / 日常价 {total / 100:.2f} 元"
    return txt


async def discover_activity_id(page):
    """从活动页自身内嵌的数据里读当前活动 id（腾讯换活动时这里会跟着变）"""
    try:
        aid = await page.evaluate("""
            () => {
                const html = document.documentElement.innerHTML;
                let m = html.match(/"big_activity_id"\\s*:\\s*"?(\\d{12,20})"?/);
                if (m) return Number(m[1]);
                m = html.match(/"activity_id"\\s*:\\s*(\\d{12,20})/);
                return m ? Number(m[1]) : 0;
            }
        """)
        return int(aid) if aid else 0
    except Exception as e:
        log(f"读取活动 id 失败: {str(e)[:60]}")
        return 0


async def fetch_price(page, activity_id, act_id, type_, region_id):
    """问一次价格。免登录可用，用来确认这个 act_id 是不是 38 元那款。

    不需要 activity_id 之外的参数，goods 里也不能带 activity_id（会 400）。
    返回 None 表示查不到（接口报错或结构变了），调用方要能容忍。
    """
    r = await api_post(
        page,
        "/dianshi/query-price",
        {
            "activity_id": int(activity_id),
            "goods": [
                {
                    "act_id": int(act_id),
                    "type": type_,
                    "goods_param": {**GOODS_PARAM, "regionId": region_id},
                }
            ],
            "preview": 0,
        },
    )
    if r.get("code") != CODE_OK:
        return None
    for item in r.get("data") or []:
        if int(item.get("act_id") or 0) == int(act_id):
            return item
    return None


def _is_lighthouse(sale):
    """场次里的 sales 是不是轻量应用服务器那条"""
    if (sale.get("product_name") or "") == "lighthouse_v5":
        return True
    return "轻量" in (sale.get("goods_name") or "")


# 选中依据的优先级：type 与价格两个条件互相印证。
# type 指 sales[].product_name，轻量应用服务器固定是 "lighthouse_v5"
# （注意别跟 goods_param.type=bundle_budget_mc_lg4_01 混了，那是规格包编号）。
RANK_DESC = {
    0: "type=lighthouse_v5 + 价格对上 38 元",
    1: "type=lighthouse_v5（价格没对上或没查到）",
    2: "价格对上 38 元 + 名字含轻量",
    3: "只有价格对上 38 元，type/名字都没认出来",
    4: "只有名字含轻量",
    9: "type、名字、价格都没对上",
}


def _rank_candidate(sale, price):
    """给场次里的商品打分，越小越像要抢的那款。

    type 来自接口字段，比商品名稳定；价格是独立的一道校验。
    两个都对上最好；只有一个对上时宁可要 type（腾讯自己声明的商品类型）。
    """
    by_type = (sale.get("product_name") or "") == "lighthouse_v5"
    by_name = "轻量" in (sale.get("goods_name") or "")
    by_price = bool(price) and price.get("real_cost") == EXPECT_REAL_COST
    if by_type and by_price:
        return 0
    if by_type:
        return 1
    if by_price and by_name:
        return 2
    if by_price:
        return 3
    if by_name:
        return 4
    return 9


async def discover_flash_sale(page, region_id=8):
    """开抢前现查：当前在售场次 + 对应的 act_id / activity_id

    腾讯每次换活动都会换 act_id，写死在代码里必然过期（换来的是 1101001）。
    这个接口不需要签名，登录后就能调。拿不到才回落到代码里的兜底值。
    返回 (goods, session)，session 为 None 表示用的是兜底值。
    """
    candidates = []
    aid = await discover_activity_id(page)
    if aid:
        candidates.append(aid)
    for a in CANDIDATE_ACTIVITY_IDS:
        if a not in candidates:
            candidates.append(a)

    now = time.time()
    for act in candidates:
        r = await api_post(
            page,
            "/dianshi/query-seckill-by-date",
            {
                "activity_id": act,
                "preview": 0,
                "seckill_type": "block",
                "goods_type": "goods",
                "days": 7,
            },
        )
        if r.get("code") != CODE_OK:
            log(f"  活动 {act}: 查不到场次 (code={r.get('code')})")
            continue

        upcoming, past = [], []
        for date_str, day in ((r.get("data") or {}).get("flashResult") or {}).items():
            for s in day or []:
                try:
                    start = datetime.strptime(
                        f"{date_str} {s.get('start_time')}", "%Y-%m-%d %H:%M:%S"
                    )
                except (ValueError, TypeError):
                    continue
                (upcoming if start.timestamp() > now else past).append((date_str, s, start))
        upcoming.sort(key=lambda x: x[2])
        past.sort(key=lambda x: x[2])

        for date_str, s, start in upcoming[:1] or past[-1:]:
            sales = s.get("sales") or []
            # 整场商品都问一遍价格。非轻量的商品会报 1100109（goods_param 不属于它们），
            # 拿不到价格就排到最后，不影响判断。
            cands = []
            for sale in sales:
                if sale.get("act_id") is None:
                    continue
                typ = sale.get("product_name") or ""
                goods = {
                    "act_id": int(sale["act_id"]),
                    "activity_id": int(act),
                    "type": typ or "lighthouse_v5",
                    "name": "轻量4核4G3M (38元/年)",
                    "goods_param": {**GOODS_PARAM, "regionId": region_id},
                }
                price = await fetch_price(page, act, goods["act_id"], goods["type"], region_id)
                cands.append((_rank_candidate(sale, price), goods, price, sale))

            if not cands:
                continue  # 这场没拿到任何商品，看下一场
            cands.sort(key=lambda c: c[0])
            rank, goods, price, _sale = cands[0]
            why = RANK_DESC.get(rank, "都没对上")
            if price and price.get("real_cost") != EXPECT_REAL_COST:
                why += f"，价格是 {fmt_price(price)}"

            goods["price"] = price
            goods["price_check"] = why
            session = {
                "id": s.get("id"),
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": f"{date_str} {s.get('end_time')}",
                "started": start.timestamp() <= now,
            }
            return goods, session

    log("自动发现失败，使用代码里的兜底 id（可能已过期，会返回 1101001）")
    log("  排查：1) 活动页是否换了地址 → 改 ACTIVITY_URL")
    log("        2) 是否已换新活动 → 把新 activity_id 加进 CANDIDATE_ACTIVITY_IDS")
    fallback = {**FLASH_SALE, "goods_param": {**FLASH_SALE["goods_param"], "regionId": region_id}}
    price = await fetch_price(
        page, fallback["activity_id"], fallback["act_id"], fallback["type"], region_id
    )
    fallback["price"] = price
    if price and price.get("real_cost") != EXPECT_REAL_COST:
        fallback["price_check"] = "兜底 id 价格对不上"
        log(f"  注意：兜底商品是 {fmt_price(price)}，不是 38 元")
    else:
        fallback["price_check"] = "兜底 id" if price else "兜底 id，价格未校验"
    return fallback, None
