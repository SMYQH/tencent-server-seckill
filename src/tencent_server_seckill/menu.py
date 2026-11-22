#!/usr/bin/env python3
"""
腾讯云服务器抢购助手（控制台版）— bat 一键启动
============================================
活动: 上云精选·限时秒杀（轻量应用服务器 38元/年）
网址: https://cloud.tencent.com/act/pro/featured-202607#MS

菜单里选地域、扫码登录、开抢。登录态存在 .state.json，下次直接复用。

关于登录态（踩坑记录，改之前先看这段）：
  - 腾讯的 skey 是会话 cookie（expires = -1）。Playwright 的 storageState()
    **会**导出它，new_context(storage_state=...) 也**能**恢复它 —— 已实测。
    所以"关浏览器就必然要重扫"不是机制限制。
  - 真正容易出问题的是两个时机/判定：
    (1) 扫码成功后 cookie 有几秒延迟，存早了就是一份没有 skey 的空壳；
        -> 用 _wait_login_cookies() 等 skey 落地再存。
    (2) 扫码后 skey 会换新，而页面里缓存的 csrf 还是登录**前**的旧 token，
        服务端于是回 CSRF-ERROR。它既不是"已登录"，也不是"失效"，而是必须
        拿当前 skey 用 g_tk 重算才能消掉的状态 -> _check_login() 里 force 重算。
        在这个状态存 state 服务端不认（issue #13 的假成功就是这么来的）。
免责声明: 仅供个人学习使用；自动化抢购可能违反活动规则，
可能致账号限流/受限，风险自担；请勿高频滥用。
"""

import asyncio

from .browser import ensure_chromium
from .console import Fore, Style, c
from .login import do_login
from .settings import (
    REGION_NAMES,  # 复用 settings.py 那一份，别各写各的（改一处就够）
    session_profile,
)
from .storage import _state_summary, load_config, save_config
from .workflows import run_grab, test_once

# ---------- 菜单 ----------


def show_menu():
    cfg = load_config()
    region = cfg.get("region", 8)
    rotate = cfg.get("rotate", True)
    state_desc = _state_summary()
    c("=" * 56, Fore.YELLOW)
    c("        腾讯云服务器抢购助手", Fore.YELLOW + Style.BRIGHT)
    c("  活动: 上云精选·限时秒杀（轻量应用服务器，每天 10:00 / 15:00）", Fore.WHITE)
    c("  商品 id 每期活动会变，开始抢购时自动现查，无需手动改", Fore.WHITE)
    c("=" * 56, Fore.YELLOW)
    c(f"  登录状态  : {state_desc}", Fore.CYAN)
    p10 = session_profile("10:00")
    p15 = session_profile("15:00")
    c(
        f"  同时请求  : 10:00场 {p10['cap_start']}→{p10['cap_max']} · "
        f"15:00场 {p15['cap_start']}→{p15['cap_max']}（按实测吞吐自动加，不用设）",
        Fore.CYAN,
    )
    c(f"  持续时间  : 10:00场 {p10['duration']}s · 15:00场 {p15['duration']}s", Fore.CYAN)
    c("-" * 56, Fore.WHITE)
    c(
        f"  [1] 地域设置（当前: {'三地轮动并发' if rotate else '只抢' + REGION_NAMES.get(region, str(region))}）",
        Fore.WHITE,
    )
    c("  [2] 扫码登录（首次或登录过期时）", Fore.WHITE)
    c("  [3] 开始抢购（自动找最近场次 10:00/15:00）", Fore.WHITE)
    c("  [4] 选择场次抢购（10:00 / 15:00）", Fore.WHITE)
    c("  [5] 测试下单流程（不付款；测完取消订单，不要反复测）", Fore.WHITE)
    c("  [0] 退出", Fore.WHITE)
    c("-" * 56, Fore.WHITE)
    c("提示: 扫码后紧接着开抢，中间不要关闭弹出的浏览器窗口。", Fore.CYAN)
    c("免责声明: 本工具仅供个人学习使用；自动化抢购可能违反", Fore.YELLOW)
    c("  活动规则，可能导致账号限流/受限，风险自担；请勿高频滥用。", Fore.YELLOW)
    c("=" * 56, Fore.YELLOW)


def region_menu():
    """地域设置：三地轮动并发，或只抢其中一个。原来拆成 [1] 和 [6] 两个入口，太远，合到这里。"""
    cfg = load_config()
    rotate = cfg.get("rotate", True)
    region = cfg.get("region", 8)
    c("-" * 56, Fore.WHITE)
    c("  地域设置", Fore.CYAN)
    c("  [1] 三地轮动并发（默认，推荐）", Fore.WHITE)
    c("  [2] 只抢上海", Fore.WHITE)
    c("  [3] 只抢北京", Fore.WHITE)
    c("  [4] 只抢广州", Fore.WHITE)
    c("  [0] 返回，不改", Fore.WHITE)
    cur = "三地轮动并发" if rotate else f"只抢{REGION_NAMES.get(region, region)}"
    c(f"  当前: {cur}", Fore.YELLOW)
    c("-" * 56, Fore.WHITE)
    try:
        k = input("  请选择: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if k == "1":
        cfg["rotate"] = True
        cfg.pop("region", None)  # 轮动时主地域没意义，别留个死值误导自己
        save_config(cfg)
        c("  已设为三地轮动并发：上海、北京、广州一起抢，某个地域售罄就不再去它那里。", Fore.GREEN)
    elif k in ("2", "3", "4"):
        rid = {"2": 4, "3": 8, "4": 1}[k]
        cfg["rotate"] = False
        cfg["region"] = rid
        save_config(cfg)
        c(f"  已设为只抢{REGION_NAMES[rid]}。另外两个地域不再发请求。", Fore.GREEN)
    elif k == "0":
        c("  未改动。", Fore.YELLOW)
    else:
        c("  无效输入，未改动。", Fore.RED)


def _menu_loop():
    while True:
        show_menu()
        try:
            choice = input("请输入: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        cfg = load_config()
        region = cfg.get("region", 8)
        rotate = bool(cfg.get("rotate", True))
        if choice == "1":
            region_menu()
        elif choice == "2":
            asyncio.run(do_login())
        elif choice == "3":
            asyncio.run(run_grab(region, rotate=rotate))
        elif choice == "4":
            c("  场次选项: [1] 10:00 场  [2] 15:00 场", Fore.CYAN)
            t = input("  选择场次编号: ").strip()
            if t == "1":
                asyncio.run(run_grab(region, target_time="10:00", rotate=rotate))
            elif t == "2":
                asyncio.run(run_grab(region, target_time="15:00", rotate=rotate))
            else:
                c("  无效输入（请输入 1 或 2）", Fore.RED)
        elif choice == "5":
            asyncio.run(test_once(region))
        elif choice == "0":
            break
        else:
            c("  无效选项", Fore.RED)
        try:
            input("\n按回车继续...")
        except (EOFError, KeyboardInterrupt):
            break
    c("再见！", Fore.YELLOW)


def main():
    try:
        ensure_chromium()
        _menu_loop()
    except KeyboardInterrupt:
        # Ctrl+C 中断（如等待场次时想提前退出）：不再打整页 traceback
        c("\n已中断退出。登录态等进度保存在本地，下次可直接使用。", Fore.YELLOW)
