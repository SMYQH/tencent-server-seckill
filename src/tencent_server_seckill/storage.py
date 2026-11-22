import json
from datetime import datetime
from pathlib import Path

_PACKAGE_PARENT = Path(__file__).resolve().parent.parent
DATA_DIR = _PACKAGE_PARENT.parent if _PACKAGE_PARENT.name == "src" else Path.cwd()
STATE_PATH = DATA_DIR / ".state.json"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "seckill.log"
UI_LOG_PATH = DATA_DIR / "seckill_ui.log"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}")
    try:
        # 中文 Windows 默认 cp936，编不了 emoji，不指定 encoding 会 UnicodeEncodeError
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except UnicodeEncodeError:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")


def load_config():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        config = None
    return config if isinstance(config, dict) else {"region": 8}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _state_missing_skey():
    """文件存在但里面没有 skey/p_skey -> True。损坏文件也按"缺"处理。"""
    if not STATE_PATH.exists():
        return False
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return True
    cs = [c for c in (data.get("cookies") or []) if isinstance(c, dict)]
    names = {c.get("name") for c in cs}
    return not ("skey" in names or "p_skey" in names)


def _state_summary():
    """读 .state.json 报一句实话：到底存到了 skey 没有。

    以前只判断"文件在不在"，文件在但里面没有 skey 时也显示"已保存"，
    用户就放心去关浏览器了，下次又得重扫。字符串必须如实反映内容。
    """
    if not STATE_PATH.exists():
        return "未登录"
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "文件损坏，需重扫"
    cs = [c for c in (data.get("cookies") or []) if isinstance(c, dict)]
    names = {c.get("name") for c in cs}
    if "skey" in names or "p_skey" in names:
        return f"已保存（{len(cs)} 条 cookie，用时自动校验）"
    return f"不完整（{len(cs)} 条，缺 skey），建议重新扫码"
