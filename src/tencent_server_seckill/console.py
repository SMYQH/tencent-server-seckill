import sys
from datetime import datetime

from .storage import UI_LOG_PATH

try:
    from colorama import Fore, Style, init

    init(autoreset=True)
except ImportError:

    class _F:
        RED = GREEN = YELLOW = CYAN = WHITE = MAGENTA = RESET = ""

    class _S:
        BRIGHT = RESET_ALL = ""

    Fore, Style = _F, _S


# ---------- 基础工具 ----------
# 心跳行宽度：打印正式日志前先擦掉这一行，否则会和日志叠在同一行
HB_WIDTH = 72


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    sys.stdout.write("\r" + " " * HB_WIDTH + "\r")
    print(f"[{ts}] {msg}")
    with open(UI_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def c(msg, color=Fore.CYAN):
    print(color + msg)
