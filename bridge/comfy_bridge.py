# -*- coding: utf-8 -*-
"""ComfyUI 子链桥接核心（comfy-* 系列 Skill 共用）。

职责单一：把一次出图请求做完做透——
    读链配置 -> 预检 -> 翻译/拼装提示词 -> 注参数 -> 上传底图
    -> /prompt 提交 -> /history 轮询 -> /view 下载 -> 落盘 + meta.json -> （可选）G2 打标

不启停 ComfyUI 进程、不装模型、不改工作流结构。

用法（通常由各 Skill 的 scripts/run.py 调用，不直接给用户用）：
    python comfy_bridge.py --config <chain.json> --check
    python comfy_bridge.py --config <chain.json> -i 底图.png -p "一句话需求"
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid


# ---------------------------------------------------------------- 解释器自举

def _bootstrap():
    """系统 python 通常没有 requests/PIL，自动切到 ComfyUI 的 venv 重跑。

    重跑的是**入口脚本**（sys.argv[0]），不是本文件——子链的 run.py 会 import 本模块，
    若这里硬编码 __file__，重跑后会丢掉 run.py 的链配置参数。
    """
    try:
        import requests  # noqa: F401
        import PIL  # noqa: F401
        return
    except ImportError:
        pass
    cand = os.environ.get("COMFY_PY") or r"D:\ComfyUI\.venv\Scripts\python.exe"
    if (os.path.isfile(cand)
            and os.path.abspath(sys.executable).lower() != os.path.abspath(cand).lower()):
        raw = sys.argv[0] if sys.argv else ""
        if not raw or raw.startswith("-") or not os.path.isfile(raw):
            # 交互式 / python -c / python -m：没有可靠的入口可重跑，交给调用方处理
            return
        sys.exit(subprocess.call([cand, os.path.abspath(raw)] + sys.argv[1:]))
    raise SystemExit("[X] 缺少 requests / Pillow，且找不到 ComfyUI venv：%s" % cand)


# 被 run.py import 时也要自举：否则 requests 是 None，跑到 session() 才崩
try:
    import requests as _probe  # noqa: F401
    import PIL as _probe_pil  # noqa: F401
except ImportError:
    _bootstrap()

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

try:
    import requests
    from PIL import Image, ImageDraw
except ImportError:  # 被 import 时也要能用（run.py 已经在 venv 里）
    requests = None
    Image = None
    ImageDraw = None

SERVER = os.environ.get("COMFY_SERVER", "http://127.0.0.1:8188").rstrip("/")
TOOLBOX = os.environ.get("COMFY_TOOLBOX", r"D:\ComfyUI\landscape_toolbox")
SKILLS_ROOT = os.environ.get("WORKBUDDY_SKILLS") or os.path.join(
    os.path.expanduser("~"), ".workbuddy", "skills")
GATE = os.path.join(SKILLS_ROOT, "construction-safety-gate", "scripts", "gate.py")
PROJECTS_ROOT = os.environ.get("LANDSCAPE_PROJECTS", r"D:\workplace\设计项目")
UNFILED = "_未归档AI出图"

# 服务端 output 已从安装目录拆出（启动 bat 里 --output-directory 指的就是这里）：
# 模型与产物分家，output 只当通道，成图立刻复制进项目目录，所以这里可以随便清。
OUT_ROOT = os.environ.get("COMFY_OUTPUT_ROOT", r"D:\workplace\_AI出图缓存\comfy_output")
IN_ROOT = os.environ.get("COMFY_INPUT_ROOT", r"D:\AI\ComfyUI\Cache\input")

# 启停归用户：桌面上有现成的两个快捷方式，助手只负责探测端口、报状态。
# 指向的是 bat 文件名，与 ComfyUI 版本无关——升级框架不用重做快捷方式。
LAUNCH_LNK = "桌面「启动 ComfyUI」"
STOP_LNK = "桌面「停止 ComfyUI」"
LAUNCH_BAT = r"D:\ComfyUI\启动ComfyUI.bat"

# 底模别名，与 D:\ComfyUI\landscape_toolbox\toolbox.py 的 MODEL_ALIASES 保持一致。
# 四个都是本机已装的 SDXL：换模型不下载，只换 CheckpointLoaderSimple 里的文件名。
MODEL_ALIASES = {
    "juggernaut": "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
    "realvis": "RealVisXL_V5.0_fp16.safetensors",
    "turbo": "DreamShaperXL_Turbo_v2_1.safetensors",
    "sdxl": "sd_xl_base_1.0.safetensors",
}
# turbo 类模型 8 步就收敛，按常规 20 步跑纯属浪费时间
FAST_HINTS = ("turbo", "lightning", "lcm", "hv-")

# ---------------------------------------------------------------- 显存预算
# 8 GB 卡上唯一真会伤机器的事，不是出图，是显存打满后走 WDDM 共享内存换页：
# 一次能写几十 GB 页面文件、卡 20 分钟、GPU 全程满载，代价是正常出图的上万倍。
# 下面系数是 2026-11-13 在本机逐点实测反解出来的，不是估的。三个实测点：
#   0 控制网 / 1.05 MP -> 峰值 5.2 GB      2 控制网 / 1.05 MP -> 6.9 GB      2 控制网 / 0.39 MP -> 6.5 GB
# 三点解出 BASE=4.56 / PER_CN=0.85 / PER_MP=0.61，与实测逐点吻合在 0.05 GB 内。
# 关键认知（旧参数恰好搞反）：显存大头是**模型常驻**，几乎不随像素涨——
# 像素翻 2.7 倍显存只多 6%，因为注意力是分块算的。所以旧公式小图低估（误判安全）、
# 大图高估（把能跑 1.5 MP 的图砍到 0.9 MP，白丢清晰度）。
VRAM_BASE = 4.60         # 底模 + CLIP + VAE + CUDA context 常驻（GB）
VRAM_PER_CN = 0.85       # 每个 ControlNet 的权重与中间激活（三点拟合确认，不用改）
VRAM_PER_MP = 0.65       # 每百万像素的注意力 / latent 增量（实测 0.606，取 0.65 留余量）
VRAM_RESERVE = 0.70      # 留给系统与桌面的余量，不许动用
VRAM_MIN_SIDE = 768      # 再缩就出不了活，缩到这里停手

# 换模型要整份重读权重（6.5 GB），一批图固定一个底模最省
STATE_FILE = os.path.join(TOOLBOX, ".cache", "bridge_state.json")

# 出图代价账本：每出一次图追加一行。不记这个，"低代价"就只是口号——
# 到底贵在分辨率、步数还是换模型，只能靠数字看。
COST_LOG = os.path.join(os.path.dirname(OUT_ROOT), "出图代价.jsonl")

# 出图档位：用户只说用途（先看看 / 汇报 / 交甲方），参数由这里定，不再让用户挑开关。
# 三档都不换底模。实测换一次只多约 20 s、页面文件峰值 566 MB（读盘为主，不磨损 SSD），
# 所以这不是「伤机器」问题，而是「一批图固定一个模型能省 20 s」的效率问题——
# 不值得为它把档位搞复杂。真要换（turbo 批量试、realvis 追质感）由助手显式指定。
# 三档**同尺寸**：草稿与定稿分辨率一致，构图才可迁移——否则"看构图"看出的是另一张图，
# 定稿还要重挑一次，反而更贵。
TIERS = {
    "draft": {
        "label": "试方案", "model": None, "steps": 8, "cfg": None,
        "long_side": None, "gate": False, "suffix": "__草稿",
        "why": "同底模 / 8 步 / 与定稿同尺寸：约 40% 时间，构图可直接拿去定稿",
    },
    "review": {
        "label": "汇报稿", "model": None, "steps": None, "cfg": None,
        "long_side": None, "gate": True, "suffix": "",
        "why": "链路标定值（20 步 / 长边 1152–1216）：能贴进汇报、能发群里",
    },
    "final": {
        "label": "定稿", "model": None, "steps": 24, "cfg": None,
        "long_side": None, "gate": True, "suffix": "__定稿",
        "why": "同底模 / 24 步 / 原标定尺寸：只给要交人的那一张",
    },
}

# 多角度链没有角度节点，视角靠提示词里的英文方位词表达
ANGLE_WORDS = {
    "front": "front view",
    "side": "side view",
    "back": "back view",
    "aerial": "aerial view",
    "top": "top view",
    "eye": "eye level view",
}

# 交付语境词：出现即认为这批图会被当作施工/报建依据，必须过闸门
DELIVERY_WORDS = ["施工图", "报建", "报审", "送审", "下料", "开料", "材料下单",
                  "可用于施工", "照着施工", "按这个施工", "盖章", "验收", "竣工",
                  "交付", "发甲方", "出图给甲方", "施工深化"]

# 视觉稿信号：图种像施工图，但用途是汇报/沟通。这一类可以出图，但必须烧"视觉稿"标。
VISUAL_WORDS = ["视觉稿", "示意图", "汇报", "投标", "给甲方看", "方案沟通",
                "出个样子", "示意一下", "概念示意"]

EXIT = {"OK": 0, "CONFIG": 2, "SUBMIT": 3, "NODES": 4,
        "EXEC": 5, "NOIMAGE": 6, "TIMEOUT": 7, "REFUSE": 8, "OFFLINE": 9}


class BridgeError(Exception):
    def __init__(self, code, msg, detail=""):
        Exception.__init__(self, msg)
        self.code = code
        self.msg = msg
        self.detail = detail


# ---------------------------------------------------------------- 基础工具

def load_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(path, obj):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2))


def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def session():
    s = requests.Session()
    s.trust_env = False          # 本机代理会把 127.0.0.1 也拦下来
    return s


def online(s):
    try:
        return s.get(SERVER + "/system_stats", timeout=5).status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------- 资源账本

def gpu_stats(s=None):
    """读一次显存/内存实况；服务不在就返回 None，不影响出图。"""
    try:
        r = (s or session()).get(SERVER + "/system_stats", timeout=5).json()
        dev = (r.get("devices") or [{}])[0]
        total = (dev.get("vram_total") or 0) / 2.0 ** 30
        if not total:
            return None
        return {"name": dev.get("name") or "GPU",
                "total": total,
                "free": (dev.get("vram_free") or 0) / 2.0 ** 30,
                "ram_free": (r.get("system", {}).get("ram_free") or 0) / 2.0 ** 30}
    except Exception:
        return None


def vram_budget(st):
    """本次能用的显存（GB）。按物理总量算——换模型时 ComfyUI 自己会把上一个卸掉，
    不能把「当前被占的」当成永久占用，否则每次换底模都会被误判成显存不足。"""
    return max(VRAM_BASE + VRAM_PER_MP * 0.6, st["total"] - VRAM_RESERVE)


def peak_vram(mp, cn):
    return VRAM_BASE + VRAM_PER_CN * cn + VRAM_PER_MP * mp


def shrink_to_budget(w, h, cn, budget):
    """超预算就等比缩，长边不低于 768。返回 (宽, 高, 预计峰值)。"""
    mp = w * h / 1e6
    peak = peak_vram(mp, cn)
    if peak <= budget:
        return w, h, peak
    # 只吃 94% 预算：估算本身有误差，贴着上限跑等于赌它不准
    room = budget * 0.94 - VRAM_BASE - VRAM_PER_CN * cn
    if room <= 0.2:                          # 极限情况也不许算出 0 尺寸
        room = 0.2
    scale = (room / (VRAM_PER_MP * mp)) ** 0.5
    nw = int(round(w * scale / 8)) * 8
    nh = int(round(h * scale / 8)) * 8
    nw, nh = max(64, nw), max(64, nh)
    if max(nw, nh) < VRAM_MIN_SIDE:          # 保住长边下限，宁紧勿糊
        k = float(VRAM_MIN_SIDE) / max(nw, nh)
        nw, nh = max(64, int(round(nw * k / 8)) * 8), max(64, int(round(nh * k / 8)) * 8)
    return nw, nh, peak_vram(nw * nh / 1e6, cn)


def eta_seconds(steps, mp, cn):
    """粗估采样耗时（秒）。只是给用户一个量级，不当承诺。"""
    return 3 + steps * (0.45 + 0.28 * mp) * (1 + 0.25 * cn)


def note_checkpoint(ckpt):
    """记下上一张用的底模：换模型要整份重读，一批图最好别来回换。"""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        prev = load_json(STATE_FILE) if os.path.isfile(STATE_FILE) else {}
    except Exception:
        prev = {}
    old = prev.get("model")

    def short(n):
        return os.path.splitext(os.path.basename(n or ""))[0].split("_")[0]

    if old and old != ckpt:
        # 实测（2026-11-13）：换模型全程页面文件峰值仅 566 MB，且 sd 权重走 mmap 按需读页，
        # 是「读盘」不是「写盘」——读取不消耗 SSD 寿命。所以这里只提时间，不再拿磨损吓人。
        print("换底模：%s → %s（重读 6.5 GB 权重，首批多约 20 s；不写盘、不伤 SSD，只是慢）"
              % (short(old), short(ckpt)))
    try:
        prev.update({"model": ckpt, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        dump_json(STATE_FILE, prev)
    except Exception:
        pass


def mem_snapshot():
    """物理内存与提交量的即时快照（GB）。

    为什么盯这个而不是页面文件大小：本机页面文件是预分配的 16 GB 固定文件，
    看文件大小永远是 16 GB，看不出用没用。真正决定「会不会换页」的是
    **物理内存还剩多少**——16 GB 机器跑 SDXL，可用掉到 1 GB 以下就开始往盘上倒。
    """
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None
        g = float(2 ** 30)
        return {"phys_total": ms.ullTotalPhys / g, "phys_free": ms.ullAvailPhys / g,
                "commit_total": ms.ullTotalPageFile / g,
                "commit_used": (ms.ullTotalPageFile - ms.ullAvailPageFile) / g}
    except Exception:
        return None


def pagefile_paths():
    """页面文件在哪个盘——决定「显存打满」会不会把系统盘写满。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management")
        val, _ = winreg.QueryValueEx(key, "PagingFiles")
        sysdrive = (os.environ.get("SystemDrive") or "C:") + "\\"
        out = []
        for x in val:
            parts = x.split()
            if not parts:
                continue
            p = parts[0]
            if p.startswith("?"):                 # 系统托管时注册表写的是 ?:\pagefile.sys
                p = sysdrive + p[2:].lstrip("\\")
            out.append(p)
        return out
    except Exception:
        return []


# ---------------------------------------------------------------- 提示词

def translate(text):
    """CLIP 不认中文：走工具箱词库。缺词只提示，不阻断。"""
    if not any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return text, []
    sys.path.insert(0, TOOLBOX)
    try:
        import toolbox
        hits, missing = toolbox.translate_words(text, toolbox.load_lib())
        if hits:
            return ", ".join(hits), missing
        return text, missing
    except Exception as e:
        print("[!] 词库不可用（%s），提示词按原样提交" % e)
        return text, []
    finally:
        sys.path.remove(TOOLBOX)


def strip_cjk(text):
    """CLIP 只认英文：残留的中文（词库没收录的）留着只会稀释提示词，直接清掉。"""
    out = "".join(" " if "\u4e00" <= ch <= "\u9fff" else ch for ch in text)
    parts = [p.strip(" ,.;、，；") for p in re.split(r"[,，、;；]", out)]
    return ", ".join(p for p in parts if p)


def build_prompt(cfg, raw):
    if not raw:
        return cfg["preset_prompt"], []
    text, missing = translate(raw)
    missing = [m for m in missing if len(m) >= 2]
    text = strip_cjk(text)
    if not text:
        return cfg["preset_prompt"], missing
    tail = cfg.get("style_tail") or ""
    if tail:
        text = "%s, %s" % (text.rstrip(", "), tail)
    return dedupe_terms(text), missing


def dedupe_terms(text):
    """用户提示词与风格尾串常撞词（master plan 出现两次），去重但不改顺序。"""
    seen, out = set(), []
    for part in [x.strip() for x in text.split(",")]:
        key = part.lower()
        if part and key not in seen:
            seen.add(key)
            out.append(part)
    return ", ".join(out)


# ---------------------------------------------------------------- 出图落盘

def _norm(p):
    return os.path.normpath(os.path.abspath(p)).lower()


def detect_project(image_path):
    """从底图路径反推项目根：...\\设计项目\\<类别>\\<项目名>\\... 取类别/项目名两段。"""
    if not image_path:
        return None
    try:
        rel = os.path.relpath(os.path.abspath(image_path), PROJECTS_ROOT)
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    parts = rel.split(os.sep)
    if len(parts) < 3:          # 至少 类别/项目/文件
        return None
    return os.path.join(PROJECTS_ROOT, parts[0], parts[1])


def find_project(token):
    """--project 支持：完整路径 / 项目目录名 / 名字片段。"""
    if not token:
        return None
    if os.path.isdir(token):
        return os.path.abspath(token)
    token_low = token.lower()
    pool = []
    for cat in sorted(os.listdir(PROJECTS_ROOT)) if os.path.isdir(PROJECTS_ROOT) else []:
        cat_dir = os.path.join(PROJECTS_ROOT, cat)
        if not os.path.isdir(cat_dir):
            continue
        for proj in sorted(os.listdir(cat_dir)):
            if os.path.isdir(os.path.join(cat_dir, proj)):
                pool.append((proj, os.path.join(cat_dir, proj)))
    for name, path in pool:
        if name.lower() == token_low:
            return path
    hit = [(n, p) for n, p in pool if token_low in n.lower()]
    if len(hit) == 1:
        return hit[0][1]
    if len(hit) > 1:
        print("[!] 项目名不唯一：%s —— 用完整路径或更长的名字" % "、".join(n for n, _ in hit))
        return None
    return None


def resolve_out_dir(cfg, args, image_path):
    """产物一律进 D:\\workplace 项目目录；定不了项目就落到 _未归档AI出图/<日期>。"""
    if args.out:
        return os.path.abspath(args.out), "指定"
    proj = find_project(args.project) or detect_project(image_path)
    if proj:
        return os.path.normpath(os.path.join(proj, cfg.get("out_subdir", "出图/AI方案稿"))), proj
    return os.path.normpath(os.path.join(PROJECTS_ROOT, UNFILED, time.strftime("%Y%m%d"))), "未归档"


def safe_name(text, fallback="out"):
    bad = '<>:"/\\|?*\n\r\t'
    s = "".join("_" if c in bad else c for c in (text or "")).strip(" ._")
    return s[:40] or fallback


# ---------------------------------------------------------------- 图像

def fit_image(path, long_side, target=None):
    """target 给定时强制缩到该尺寸（遮罩必须跟底图逐像素对齐，不能各缩各的）。"""
    im = Image.open(path).convert("RGB")
    if target:
        size = target
    else:
        if not long_side or max(im.size) <= long_side:
            return path, im.size
        s = float(long_side) / max(im.size)
        size = (max(8, round(im.width * s / 8) * 8), max(8, round(im.height * s / 8) * 8))
    if im.size == size:
        return path, size
    tmp = os.path.join(TOOLBOX, "tmp")
    os.makedirs(tmp, exist_ok=True)
    tag = hashlib.md5(os.path.abspath(path).encode("utf-8")).hexdigest()[:8]
    dst = os.path.join(tmp, "_fit_%s_%s" % (tag, os.path.basename(path)))
    im.resize(size, Image.LANCZOS).save(dst)
    return dst, size


def make_box_mask(src, spec):
    """把 "x1,y1,x2,y2"（百分比 0-100）转成白矩形遮罩，省得为一小块去开 PS。"""
    try:
        vals = [float(x) for x in str(spec).replace("%", "").split(",")]
        if len(vals) != 4:
            raise ValueError
    except (TypeError, ValueError):
        raise BridgeError(EXIT["CONFIG"], "遮罩框格式应为 x1,y1,x2,y2（0-100 百分比）：%s" % spec)
    im = Image.open(src)
    W, H = im.size
    x1, y1, x2, y2 = [max(0.0, min(100.0, v)) / 100.0 for v in vals]
    box = [int(round(x1 * W)), int(round(y1 * H)), int(round(x2 * W)), int(round(y2 * H))]
    if box[2] <= box[0] or box[3] <= box[1]:
        raise BridgeError(EXIT["CONFIG"], "遮罩框面积为 0：%s" % spec)
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).rectangle(box, fill=255)
    tmp = os.path.join(TOOLBOX, "tmp")
    os.makedirs(tmp, exist_ok=True)
    dst = os.path.join(tmp, "_box_%d_%s.png"
                       % (int(time.time()), os.path.splitext(os.path.basename(src))[0]))
    m.save(dst)
    return dst


def upload_image(s, path):
    with open(path, "rb") as f:
        r = s.post(SERVER + "/upload/image",
                   files={"image": (os.path.basename(path), f, "image/png")},
                   data={"overwrite": "true", "type": "input"}, timeout=120)
    r.raise_for_status()
    d = r.json()
    return "/".join(x for x in (d.get("subfolder", ""), d["name"]) if x)


# ---------------------------------------------------------------- 工作流注入

def load_meta(cfg):
    nm_file = os.path.join(TOOLBOX, "workflows", "api", "_node_map.json")
    if not os.path.isfile(nm_file):
        raise BridgeError(EXIT["CONFIG"], "节点映射表缺失：%s" % nm_file)
    table = load_json(nm_file)
    if cfg["api"] not in table:
        raise BridgeError(EXIT["CONFIG"], "_node_map.json 里没有 %s" % cfg["api"])
    return table[cfg["api"]]


def resolve_model(spec):
    """别名 -> 文件名；返回 (文件名, 是否 turbo 类)。"""
    if not spec:
        return None, False
    key = str(spec).strip()
    name = MODEL_ALIASES.get(key.lower(), key)
    if not name.lower().endswith((".safetensors", ".ckpt")):
        raise BridgeError(
            EXIT["CONFIG"],
            "认不出模型 %s。可用别名：%s，或直接给 .safetensors 全名"
            % (spec, " / ".join(MODEL_ALIASES)))
    return name, any(h in name.lower() for h in FAST_HINTS)


def inject(wf, meta, cfg, args):
    """返回 (seed, 实际写入的参数清单)。node id 全部来自 _node_map.json，不写死。"""
    nm = meta["node_map"]
    applied = {}

    text, missing = build_prompt(cfg, args.prompt)
    if missing:
        print("[!] 词库未收录：%s（可加进 词库.json）" % "、".join(missing))
    wf[nm["positive"]["node"]]["inputs"][nm["positive"]["field"]] = text
    applied["prompt"] = text

    # 多角度链：角度靠提示词表达，没有单独节点
    angle = getattr(args, "angle", None)
    if angle:
        word = ANGLE_WORDS.get(str(angle).lower(), str(angle))
        node = wf[nm["positive"]["node"]]["inputs"]
        node[nm["positive"]["field"]] = node[nm["positive"]["field"]].rstrip(", ") + ", " + word
        applied["angle"] = word
        print("视角：%s -> %s" % (angle, word))

    neg_node = wf[nm["negative"]["node"]]["inputs"]
    extra = [x.strip() for x in (cfg.get("negative_extra") or "").split(",") if x.strip()]
    if args.negative:
        extra.append(args.negative.strip())
    if extra:
        neg_node[nm["negative"]["field"]] = (neg_node[nm["negative"]["field"]]
                                             + ", " + ", ".join(extra)).strip(", ")

    seed = args.seed if args.seed is not None else uuid.uuid4().int % (2 ** 31 - 1)
    wf[nm["seed"]["node"]]["inputs"]["seed"] = seed
    applied["seed"] = seed

    for key in ("denoise", "steps"):
        val = getattr(args, key, None)
        if val is None:
            # 链可以声明自己的默认：enhance 声明 0（纯放大），
            # 模板里留 0.2 是为了保住洗图分支，不代表默认要洗
            val = cfg.get("default_" + key)
            if val is None:
                continue
        if key not in nm:
            print("[!] 该链不支持覆盖 %s，已忽略" % key)
            continue
        wf[nm[key]["node"]]["inputs"][nm[key]["field"]] = val
        applied[key] = val

    def _set_cfg(val):
        for node in wf.values():
            if node.get("class_type") == "KSampler":
                node["inputs"]["cfg"] = val
                applied["cfg"] = val
                return True
        return False

    if getattr(args, "cfg", None) is not None:
        if not _set_cfg(args.cfg):
            print("[!] 该链没有 KSampler，已忽略 --cfg")

    # 换底模：本机四个 SDXL 都在，覆写 CheckpointLoaderSimple 即可，不下载
    ckpt_name, fast = resolve_model(getattr(args, "model", None))
    if ckpt_name:
        hit = 0
        for node in wf.values():
            if node.get("class_type") == "CheckpointLoaderSimple":
                node["inputs"]["ckpt_name"] = ckpt_name
                hit += 1
        if not hit:
            raise BridgeError(EXIT["CONFIG"], "该链没有 CheckpointLoaderSimple，无法换模型")
        applied["model"] = ckpt_name
        print("底模：%s" % ckpt_name)
        if fast:
            # 只在用户没显式指定时自动降档：turbo 类 8 步 / CFG 1.8 已收敛
            if getattr(args, "steps", None) is None and "steps" in nm:
                wf[nm["steps"]["node"]]["inputs"][nm["steps"]["field"]] = 8
                applied["steps"] = 8
            if getattr(args, "cfg", None) is None and _set_cfg(1.8):
                pass
            print("        turbo 类：8 步 / CFG 1.8（要改就显式给 --steps --cfg）")

    # 换模型有代价：记一下上一个，别在一批图里来回换
    if not getattr(args, "dry_run", False):
        for node in wf.values():
            if node.get("class_type") == "CheckpointLoaderSimple":
                note_checkpoint(node["inputs"].get("ckpt_name"))
                break

    if "save_prefix" in nm:
        wf[nm["save_prefix"]["node"]]["inputs"][nm["save_prefix"]["field"]] = "skill/" + cfg["chain"]

    # ---- 显存预算：只有采样会打满显存；denoise=0 的纯放大分支不采样，直接放过
    cn = sum(1 for n in wf.values() if n.get("class_type") == "ControlNetLoader")
    sampling = True
    if nm.get("denoise"):
        sampling = bool(wf[nm["denoise"]["node"]]["inputs"].get(nm["denoise"]["field"]))
    long_side = cfg.get("long_side") or meta.get("long_side")
    fit_side = long_side

    stats = getattr(args, "_gpu", "unset")
    if stats == "unset":
        stats = gpu_stats(getattr(args, "session", None))
        args._gpu = stats
    if sampling and stats:
        plan = None
        if args.image and os.path.isfile(args.image):
            with Image.open(args.image) as im:
                sw, sh = im.size
            if long_side and max(sw, sh) > long_side:
                k = float(long_side) / max(sw, sh)
                plan = (int(round(sw * k / 8)) * 8, int(round(sh * k / 8)) * 8)
            else:
                plan = (sw, sh)
        elif "size" in nm:
            nd = nm["size"]
            plan = (int(wf[nd["node"]]["inputs"][nd["fields"][0]]),
                    int(wf[nd["node"]]["inputs"][nd["fields"][1]]))
        if plan:
            budget = vram_budget(stats)
            nw, nh, peak = shrink_to_budget(plan[0], plan[1], cn, budget)
            mp = nw * nh / 1e6
            steps = applied.get("steps")
            eta = (" · 约 %.0f s" % eta_seconds(steps, mp, cn)) if steps else ""
            print("预算：%d×%d · %.2f MP · 控制网 %d → 峰值 %.1f / 可用 %.1f GB%s"
                  % (nw, nh, mp, cn, peak, budget, eta))
            if (nw, nh) != plan:
                print("      原尺寸 %d×%d 会摸到换页，自动降到 %d×%d（防伤机器，不是省显存）"
                      % (plan[0], plan[1], nw, nh))
                if args.image:
                    fit_side = max(nw, nh)
                else:
                    nd = nm["size"]
                    wf[nd["node"]]["inputs"][nd["fields"][0]] = nw
                    wf[nd["node"]]["inputs"][nd["fields"][1]] = nh
            applied["budget"] = "%.1f/%.1fGB" % (peak, budget)
    elif nm.get("denoise") and not sampling:
        print("预算：纯放大，不采样——显存只占解码缓存，不会换页")

    img_size = None
    if args.image:
        if "image" not in nm:
            print("[!] 该链不吃底图，已忽略 -i")
        elif getattr(args, "dry_run", False):
            applied["image_size"] = "dry-run 未上传"
            print("底图：%s（dry-run 未上传）" % os.path.basename(args.image))
        else:
            src, size = fit_image(args.image, fit_side)
            img_size = size
            name = upload_image(args.session, src)
            wf[nm["image"]["node"]]["inputs"][nm["image"]["field"]] = name
            applied["image_size"] = "%dx%d" % size
            print("底图：%s -> %dx%d" % (os.path.basename(args.image), size[0], size[1]))

    mask_path = getattr(args, "mask", None)
    box = getattr(args, "box", None)
    if box and not mask_path:
        if "mask" not in nm:
            print("[!] 该链不吃遮罩，已忽略 --box")
        elif not args.image:
            raise BridgeError(EXIT["CONFIG"], "--box 需要配合 -i 底图使用")
        elif getattr(args, "dry_run", False):
            print("遮罩：按框 %s 生成（dry-run 未生成）" % box)
        else:
            mask_path = make_box_mask(args.image, box)
            print("遮罩：按框 %s 生成矩形" % box)
    if mask_path:
        if "mask" not in nm:
            print("[!] 该链不吃遮罩，已忽略 --mask")
        elif getattr(args, "dry_run", False):
            applied["mask_size"] = "dry-run 未上传"
            print("遮罩：%s（dry-run 未上传）" % os.path.basename(mask_path))
        elif not os.path.isfile(mask_path):
            raise BridgeError(EXIT["CONFIG"], "遮罩文件不存在：%s" % mask_path)
        else:
            # 遮罩必须与底图同尺寸，否则 VAEEncodeForInpaint 会错位
            src, size = fit_image(mask_path, None, target=img_size)
            name = upload_image(args.session, src)
            wf[nm["mask"]["node"]]["inputs"][nm["mask"]["field"]] = name
            applied["mask_size"] = "%dx%d" % size
            if img_size and size != img_size:
                raise BridgeError(EXIT["CONFIG"], "遮罩与底图尺寸不一致：%s vs %s"
                                  % (size, img_size))
            print("遮罩：%s -> %dx%d" % (os.path.basename(mask_path), size[0], size[1]))

    # 风格参考图（IPAdapter）：与底图各占一个 LoadImage
    style = getattr(args, "style", None)
    if style:
        if "style_image" not in nm:
            print("[!] 该链不吃风格参考图，已忽略 --style")
        elif not os.path.isfile(style):
            raise BridgeError(EXIT["CONFIG"], "风格参考图不存在：%s" % style)
        elif getattr(args, "dry_run", False):
            applied["style_image"] = "dry-run 未上传"
            print("风格参考：%s（dry-run 未上传）" % os.path.basename(style))
        else:
            # 风格靠色彩与质感，缩太狠会丢；沿用底图同一档长边
            src, size = fit_image(style, fit_side)
            name = upload_image(args.session, src)
            wf[nm["style_image"]["node"]]["inputs"][nm["style_image"]["field"]] = name
            applied["style_image"] = "%dx%d" % size
            print("风格参考：%s -> %dx%d" % (os.path.basename(style), size[0], size[1]))

    if getattr(args, "style_weight", None) is not None:
        if "style_weight" in nm:
            wf[nm["style_weight"]["node"]]["inputs"][nm["style_weight"]["field"]] = args.style_weight
            applied["style_weight"] = args.style_weight
        else:
            print("[!] 该链没有 IPAdapter，已忽略 --style-weight")

    # 放大链的输出尺寸必须按底图比例算，否则 2048×2048 会把非方图拉变形
    if "scale_to" in nm and args.image:
        dims = img_size if isinstance(img_size, tuple) and len(img_size) == 2 else None
        if dims is None and os.path.isfile(args.image):
            with Image.open(args.image) as im:
                dims = im.size
        if dims:
            target = int(nm["scale_to"].get("target") or 2048)
            s = float(target) / max(dims)
            nw = max(8, int(round(dims[0] * s / 8)) * 8)
            nh = max(8, int(round(dims[1] * s / 8)) * 8)
            # nodes 支持一次改多个 ImageScale：enhance 有「纯放大」和「洗后放大」两个
            targets = nm["scale_to"].get("nodes") or [nm["scale_to"]["node"]]
            for t in targets:
                ins = wf[str(t)]["inputs"]
                for f, v in zip(nm["scale_to"]["fields"], (nw, nh)):
                    ins[f] = v
            applied["upscale_to"] = "%dx%d" % (nw, nh)
            print("放大目标：%dx%d（长边 %d，按底图比例）" % (nw, nh, target))

    # denoise=0 的链（放大/洗图）：删掉洗图那路的 SaveImage。
    # ComfyUI 只求值被输出节点依赖的节点，KSampler 分支整条不执行——
    # 实测过一遍 SDXL VAE 就掉到 67% 清晰度，denoise=0 也没用，所以干脆别走。
    drop = meta.get("drop_if_no_denoise") or []
    if drop:
        dn = nm.get("denoise")
        cur = wf[dn["node"]]["inputs"].get(dn["field"], 0) if dn else 0
        if not cur:
            for nid in drop:
                wf.pop(str(nid), None)
            print("denoise=0：纯放大，跳过洗图分支")

    return seed, applied, meta


# ---------------------------------------------------------------- 提交 / 轮询 / 下载

def submit(s, wf):
    r = s.post(SERVER + "/prompt", json={"prompt": wf, "client_id": uuid.uuid4().hex}, timeout=60)
    if r.status_code != 200:
        raise BridgeError(EXIT["SUBMIT"], "提交被拒 HTTP %d" % r.status_code, r.text[:1200])
    body = r.json()
    if body.get("node_errors"):
        raise BridgeError(EXIT["NODES"], "节点参数有误",
                          json.dumps(body["node_errors"], ensure_ascii=False, indent=2)[:1200])
    if not body.get("prompt_id"):
        raise BridgeError(EXIT["SUBMIT"], "提交无 prompt_id", json.dumps(body, ensure_ascii=False)[:600])
    return body["prompt_id"]


def queue_state(s, pid):
    """返回 running / pending / done / gone —— 用于早点发现任务丢了。"""
    try:
        q = s.get(SERVER + "/queue", timeout=15).json()
    except Exception:
        return "unknown"
    for key in ("queue_running", "queue_pending"):
        for item in q.get(key, []):
            if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == pid:
                return "running" if key == "queue_running" else "pending"
    return "gone"


def vram_used(s):
    """此刻显存占用（GB）。采不到就返回 0，绝不因为统计失败影响出图。"""
    try:
        st = s.get(SERVER + "/system_stats", timeout=5).json()
        dv = (st.get("devices") or [{}])[0]
        tot, free = dv.get("vram_total") or 0, dv.get("vram_free") or 0
        return (tot - free) / 2.0 ** 30 if tot else 0.0
    except Exception:
        return 0.0


def poll(s, pid, timeout, interval):
    """等任务跑完，同时每 2 秒采一次显存，记下真实峰值。

    峰值是判断「这次会不会开始换页」的唯一硬证据：估算的 budget 只是事前推演，
    峰值是事后实测。两者差太多，说明系数该重标了。
    """
    t0 = time.time()
    last = 0.0
    miss = 0
    peak = 0.0
    next_probe = 0.0
    while time.time() - t0 < timeout:
        rec = s.get("%s/history/%s" % (SERVER, pid), timeout=30).json().get(pid)
        if rec:
            status = rec.get("status", {})
            if status.get("status_str") == "error":
                raise BridgeError(EXIT["EXEC"], "执行失败",
                                  json.dumps(status, ensure_ascii=False)[:1200])
            if rec.get("outputs"):
                return rec, time.time() - t0, peak
        if queue_state(s, pid) == "gone" and not rec:
            miss += 1
            if miss >= 2:
                raise BridgeError(EXIT["NOIMAGE"], "任务在队列里消失（ComfyUI 可能重启过）")
        now = time.time() - t0
        if now >= next_probe:
            peak = max(peak, vram_used(s))
            next_probe = now + 2.0
        if now - last > 20:
            print("    ... 等待中 %.0fs（队列:%s · 显存峰值 %.1f GB）"
                  % (now, queue_state(s, pid), peak))
            last = now
        time.sleep(interval)
    raise BridgeError(EXIT["TIMEOUT"], "超时 %.0f 秒（显存峰值 %.1f GB）" % (timeout, peak))


def fetch(s, rec, labels=None):
    """下载全部输出图，返回 [(语义标签, 字节)]。

    labels 是 node_map 里的 save_labels（节点 id -> 标签），优先于从文件名猜——
    因为桥接层会统一改写 save_prefix，多输出链的文件名里就没有语义了。
    """
    labels = dict(labels or {})
    files = []
    for nid, out in rec.get("outputs", {}).items():
        tag = labels.get(str(nid), "")
        for img in out.get("images", []):
            q = {"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                 "type": img.get("type", "output")}
            r = s.get(SERVER + "/view", params=q, timeout=180)
            if r.status_code == 200:
                files.append((tag or out_tag(img["filename"]), r.content))
    if not files:
        raise BridgeError(EXIT["NOIMAGE"], "跑完了但没有图片输出")
    return files


def out_tag(filename):
    """从服务端文件名里取语义尾缀：enhance_upscaled_00001_.png -> upscaled。

    这样才能把「洗过的」和「纯放大的」分开命名，而不是 _0 / _1。
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    parts = [p for p in stem.split("_") if p and not p.isdigit()]
    return parts[-1].lower() if len(parts) >= 2 else ""


# ---------------------------------------------------------------- 代价账本

def log_cost(row):
    """追加一行代价记录。写不进去也不算错——记账不该阻断出图。"""
    try:
        d = os.path.dirname(COST_LOG)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with io.open(COST_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def cmd_cost(limit=0):
    """按「链路 × 档位」汇总历史代价：跑了几次、平均多久、峰值多高、写了多少盘。"""
    rows = []
    try:
        with io.open(COST_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except IOError:
        print("还没有代价记录。出一次图就会开始记：%s" % COST_LOG)
        return EXIT["OK"]
    if not rows:
        print("还没有代价记录：%s" % COST_LOG)
        return EXIT["OK"]
    if limit:
        rows = rows[-limit:]

    agg = {}
    for r in rows:
        k = (r.get("链路", "?"), r.get("档位", "-") or "-")
        a = agg.setdefault(k, {"n": 0, "s": 0.0, "peak": 0.0, "mb": 0.0})
        a["n"] += 1
        a["s"] += float(r.get("秒") or 0)
        a["peak"] = max(a["peak"], float(r.get("峰值显存GB") or 0))
        a["mb"] += float(r.get("产物MB") or 0)

    print("%-20s %-6s %4s %8s %9s %10s" % ("链路", "档位", "次数", "平均秒", "峰值GB", "累计写盘"))
    print("-" * 66)
    for (chain, tier), a in sorted(agg.items(), key=lambda x: -x[1]["mb"]):
        print("%-20s %-6s %4d %8.1f %9.1f %8.0f MB"
              % (chain, tier, a["n"], a["s"] / a["n"], a["peak"], a["mb"]))
    print("-" * 66)
    total_mb = sum(a["mb"] for a in agg.values())
    total_s = sum(a["s"] for a in agg.values())
    print("合计 %d 次 · %.1f 分钟 · 写盘 %.0f MB（%.2f GB）"
          % (len(rows), total_s / 60.0, total_mb, total_mb / 1024.0))
    print("账本：%s" % COST_LOG)
    return EXIT["OK"]


# ---------------------------------------------------------------- 闸门

def classify_intent(prompt):
    """判用途，不判图种。返回 (document|visual|none, 命中的词)。

    document = 要能照着做（下料/报建/施工），生成模型给不出，直接拒。
    visual   = 图种像施工图但只是汇报用，可以出，必须烧"视觉稿"标。
    none     = 纯表达。
    """
    text = prompt or ""
    doc = [w for w in DELIVERY_WORDS if w in text]
    vis = [w for w in VISUAL_WORDS if w in text]
    if doc and vis:
        return "visual", doc + vis          # 「出张节点视觉稿给甲方」优先按视觉稿
    if doc:
        return "document", doc
    if vis:
        return "visual", vis
    return "none", []


def gate_init(path, category, note, intent="document"):
    if not os.path.isfile(GATE):
        return None, "找不到 gate.py：%s" % GATE
    try:
        r = subprocess.run([sys.executable, GATE, "init", "--file", path,
                            "--category", category, "--note", note,
                            "--source", "ai", "--intent", intent],
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return None, repr(e)


def register_draft(path, cfg, prompt):
    """方案表达稿也要登记：交付包靠这条记录区分「AI 方案稿」与「无记录文件」。"""
    code, log = gate_init(path, cfg.get("category", "通用"), prompt or "", "draft")
    if code == 0:
        print("")
        print("[G2 闸门] 已登记：DRAFT_AI（方案表达稿，只进交付包 06_方案表达_AI稿/）")
        print("          说明：%s" % (os.path.splitext(path)[0] + "__方案表达稿使用说明.md"))
        return "DRAFT_AI"
    print("[!] 闸门登记异常：%s" % (log or "")[:300])
    return "INIT_FAILED"


def read_stamped(path):
    side = os.path.splitext(path)[0] + ".gate.json"
    try:
        return load_json(side).get("stamped")
    except Exception:
        return None


# ---------------------------------------------------------------- 预检 / 主流程

def preflight(cfg, args, need_image=True, need_server=True):
    rows = []
    ok = True

    clean = not policy_scan()
    rows.append(("进程策略", "只提交，不启停 ComfyUI" if clean else "源码含启停写法", clean))
    ok = ok and clean

    s = session()
    up = online(s) if need_server else True
    rows.append(("ComfyUI 服务", SERVER + ("" if need_server else "（dry-run 免检）"), up))
    ok = ok and up

    api = os.path.join(TOOLBOX, "workflows", "api", cfg["api"])
    has_api = os.path.isfile(api)
    rows.append(("API 工作流", cfg["api"], has_api))
    ok = ok and has_api

    try:
        meta = load_meta(cfg)
        rows.append(("节点映射", "_node_map.json", True))
    except BridgeError as e:
        meta = None
        rows.append(("节点映射", e.msg, False))
        ok = False

    if need_image and cfg.get("needs_image", True):
        exists = bool(args.image) and os.path.isfile(args.image)
        rows.append(("底图", args.image or "（缺）", exists))
        ok = ok and exists
    if cfg.get("needs_mask") and not args.dry_run:
        mpath, mbox = getattr(args, "mask", None), getattr(args, "box", None)
        if mpath:
            mex, shown = os.path.isfile(mpath), mpath
        elif mbox:
            mex, shown = True, "按框 %s 生成" % mbox
        else:
            mex, shown = False, "（缺）"
        rows.append(("遮罩", shown, mex))
        ok = ok and mex
    if cfg.get("needs_style") and not args.dry_run:
        spath = getattr(args, "style", None)
        sex = bool(spath) and os.path.isfile(spath)
        rows.append(("风格参考", spath or "（缺）", sex))
        ok = ok and sex

    out_dir, scope = resolve_out_dir(cfg, args, args.image)
    try:
        os.makedirs(out_dir, exist_ok=True)
        probe = os.path.join(out_dir, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        writable = True
    except Exception:
        writable = False
    rows.append(("输出目录", out_dir, writable))
    ok = ok and writable

    rows.append(("闸门脚本", "gate.py", os.path.isfile(GATE)))
    return rows, s, ok


def cmd_health():
    """只体检服务：在线/显存/队列。离线就告诉用户点哪个快捷方式，绝不自己拉进程。"""
    s = session()
    up = online(s)
    print("== ComfyUI 服务体检 ==")
    print("  地址    %s" % SERVER)
    print("  状态    %s" % ("在线" if up else "离线"))
    if not up:
        print("  下一步  双击%s（= %s）" % (LAUNCH_LNK, LAUNCH_BAT))
        print("          脚本会先看 8188 是否已被占用，不会起第二个实例；就绪后自己开浏览器。")
        print("          好了再叫我，我直接提交任务。")
        print("  暂停    %s" % STOP_LNK)
        print("  边界    启停归你：本工具只提交，不代启也不代停，显存始终在你手里。")
        return EXIT["OFFLINE"]

    st = None
    try:
        st = s.get(SERVER + "/system_stats", timeout=5).json()
        dev = (st.get("devices") or [{}])[0]
        tot, free = dev.get("vram_total") or 0, dev.get("vram_free") or 0
        if tot:
            print("  显存    %.1f / %.1f GB 已用" % ((tot - free) / 2.0 ** 30, tot / 2.0 ** 30))
        if dev.get("name"):
            print("  设备    %s" % dev["name"])
    except Exception:
        print("  显存    （读不到 system_stats，不影响出图）")

    # 服务端到底往哪个目录写：启动参数是唯一权威证据，比猜目录可靠。
    # bat 改了没重启时，这里会明确点出来，不至于以为已经搬完。
    if st:
        argv = (st.get("system") or {}).get("argv") or []
        cur = None
        for i, a in enumerate(argv):
            if a == "--output-directory" and i + 1 < len(argv):
                cur = argv[i + 1]
            elif a.startswith("--output-directory="):
                cur = a.split("=", 1)[1]
        if cur and os.path.normpath(cur) != os.path.normpath(OUT_ROOT):
            print("  输出    %s" % cur)
            print("        [!] 不是约定位置，重启后应指向 %s" % OUT_ROOT)
        elif cur:
            print("  输出    %s" % cur)
        else:
            print("  输出    安装目录 output/（[!] 启动脚本参数未生效，需重启服务）")
            print("        重启后应指向 %s" % OUT_ROOT)

        # 两个「少伤机器」的开关是否真的上了。改了 bat 不重启，这里会照实说没生效。
        def _opt(name, default=""):
            for i, a in enumerate(argv):
                if a == name and i + 1 < len(argv):
                    return argv[i + 1]
                if a.startswith(name + "="):
                    return a.split("=", 1)[1]
            return default

        prev = _opt("--preview-method", "auto（默认）")
        rsv = _opt("--reserve-vram", "未设")
        ok_prev = (prev == "none")
        print("  预览    %s%s" % (prev, "" if ok_prev else "   [!] 未关，每步都在写 temp 小图"))
        print("  余量    %s%s" % (rsv, "" if rsv != "未设" else "   [!] 建议 0.6 GB，防显存打满换页"))

    try:
        q = s.get(SERVER + "/queue", timeout=5).json()
        print("  队列    运行 %d / 排队 %d"
              % (len(q.get("queue_running") or []), len(q.get("queue_pending") or [])))
    except Exception:
        pass

    # 磁盘：真正伤硬盘的不是出图，是显存打满后换页往系统盘猛写
    for d in ("C:\\", "D:\\"):
        try:
            u = shutil.disk_usage(d)
            print("  磁盘 %s 剩 %.0f / %.0f GB" % (d, u.free / 2.0 ** 30, u.total / 2.0 ** 30))
            if d.startswith("C") and u.free < 20 * 2 ** 30:
                print("        [!] 系统盘偏紧：一旦换页会把它写满，页面文件建议挪到 D")
        except Exception:
            pass
    pfs = pagefile_paths()
    if pfs:
        print("  页面文件 %s" % "、".join(pfs))
        if any(p.upper().startswith("C") for p in pfs):
            print("        [!] 在系统盘：显存一打满就往这里写，这是唯一会真伤盘的动作")
    m = mem_snapshot()
    if m:
        print("  内存    %.1f / %.1f GB 可用 · 提交 %.1f / %.1f GB"
              % (m["phys_free"], m["phys_total"], m["commit_used"], m["commit_total"]))
        if m["phys_free"] < 1.0:
            print("        [!] 可用内存不足 1 GB：出图极易触发换页，先关掉浏览器/微信再出")
        elif m["phys_free"] < 2.0:
            print("        内存偏紧：出图时留意「等待中」那行的显存峰值，贴着上限就别再加大图")

    print("  收工    要停服务就双击%s，别在任务管理器里猜。" % STOP_LNK)
    return EXIT["OK"]


# 只清本工具自己产的中间件：缩放副本、按框生成的遮罩、服务端输出副本。
# 真正的成图在项目目录里，这里删不到。
SWEEP_TARGETS = [
    (os.path.join(TOOLBOX, "tmp"), ("_fit_", "_box_")),
    (os.path.join(OUT_ROOT, "skill"), None),
    (IN_ROOT, ("_fit_", "_box_")),
]


def cmd_sweep(days=7, apply=False):
    """报告（默认）/ 清理（--apply）中间件。默认只读，删要明说。"""
    cutoff = time.time() - days * 86400
    print("== 中间件盘点（%d 天前） ==" % days)
    rows, total_n, total_b = [], 0, 0
    for root, prefixes in SWEEP_TARGETS:
        if not os.path.isdir(root):
            continue
        n = b = 0
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            if prefixes and not name.startswith(prefixes):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                continue
            n += 1
            b += st.st_size
            if apply:
                try:
                    os.remove(path)
                except OSError:
                    n -= 1
                    b -= st.st_size
        rows.append((root, n, b))
        total_n += n
        total_b += b
    for root, n, b in rows:
        print("  %-46s %4d 个 / %7.1f MB" % (root, n, b / 2.0 ** 20))
    print("  %-46s %4d 个 / %7.1f MB" % ("合计", total_n, total_b / 2.0 ** 20))
    if not total_n:
        print("  没有可清的东西——本来就没攒下。")
        return EXIT["OK"]
    if apply:
        print("已删除。真成图在项目目录里，不受影响。")
    else:
        print("只是盘点，没删。要清就加 --apply。")
    return EXIT["OK"]


def apply_tier(cfg, args):
    """档位 -> 具体参数。用户只说用途，开关在这里落，不摊给用户。

    默认 draft：用户定的规矩是「先草稿后定稿」——没明说用途时先出便宜的，
    认可了再由助手显式走 review / final。纯放大是后期，不是生成，不套档位。
    """
    args._tier = None
    if cfg.get("default_denoise") == 0:
        return
    name = "draft" if getattr(args, "draft", False) else (getattr(args, "tier", None) or "draft")
    t = TIERS.get(name)
    if not t:
        raise BridgeError(EXIT["CONFIG"], "档位只有 draft / review / final：%s" % name)
    args._tier = name
    if getattr(args, "model", None) is None and t["model"]:
        args.model = t["model"]
    if getattr(args, "steps", None) is None and t["steps"]:
        args.steps = t["steps"]
    if getattr(args, "cfg", None) is None and t["cfg"]:
        args.cfg = t["cfg"]
    if t["long_side"]:
        cfg["long_side"] = min(cfg.get("long_side") or t["long_side"], t["long_side"])
    print("档位：%s —— %s" % (t["label"], t["why"]))


def cmd_check(cfg, args):
    rows, _, ok = preflight(cfg, args)
    print("== %s 预检 ==" % cfg["skill"])
    for label, value, flag in rows:
        print("  [%s] %-10s %s" % ("✓" if flag else "✗", label, value))
    print("---> %s" % ("READY" if ok else "NOT_READY"))
    return EXIT["OK"] if ok else (EXIT["OFFLINE"] if not online(session()) else EXIT["CONFIG"])


def cmd_run(cfg, args):
    apply_tier(cfg, args)
    rows, s, ok = preflight(cfg, args, need_server=not args.dry_run)
    if not ok:
        for label, value, flag in rows:
            if not flag:
                print("[X] %s：%s" % (label, value))
        if rows[0][0] == "进程策略" and not rows[0][2]:
            return EXIT["REFUSE"]
        if not rows[1][2]:
            print("    ComfyUI 没在监听。双击%s（= %s）。" % (LAUNCH_LNK, LAUNCH_BAT))
            print("    它会先查 8188，已在跑就直接复用；就绪后自己开浏览器，再叫我。")
            print("    本工具不代你启动，也不会把它关掉——常驻服务由你掌握显存。")
            return EXIT["OFFLINE"]
        return EXIT["CONFIG"]

    intent, intent_hits = classify_intent(args.prompt)
    if intent == "document":
        print("[X] 这是施工图语境（%s），AI 不出能照着施工的图。" % "、".join(intent_hits))
        print("    要施工图：回 CAD/BIM 出图，本工具只做表现。")
        print("    只是想要一张给甲方看的视觉稿？把话说清：「出张节点视觉稿 / 示意图」。")
        return EXIT["REFUSE"]
    if intent == "visual":
        print("[i] 判定为施工图视觉稿（%s）：可以出图，但会烧「AI 视觉稿·不可作为施工依据」标。"
              % "、".join(intent_hits))

    wf = load_json(os.path.join(TOOLBOX, "workflows", "api", cfg["api"]))
    seed, applied, meta = inject(wf, load_meta(cfg), cfg, args)
    out_dir, scope = resolve_out_dir(cfg, args, args.image)

    rng = cfg.get("denoise_range")
    if rng and applied.get("denoise") is not None:
        if not (rng[0] <= applied["denoise"] <= rng[1]):
            print("[!] denoise %.2f 超出该链标定区间 %.2f–%.2f，效果可能跑偏"
                  % (applied["denoise"], rng[0], rng[1]))

    if args.dry_run:
        if not cfg.get("_selected"):
            print("链路：%s（%s）" % (cfg["label"], cfg["chain"]))
        print("提示词：%s" % applied.get("prompt", "")[:200])
        print("种子：%d" % seed)
        print("落盘：%s" % out_dir)
        print("[dry-run] 未提交。")
        return EXIT["OK"]

    # cmd_run 也可能被 render.py 直接调用，args 未必经过 main() 的默认值填充
    retries = getattr(args, "retries", None)
    if retries is None:
        retries = cfg.get("retries", 2)
    attempts = max(1, int(retries) + 1)
    last_err = None
    peak = 0.0
    for i in range(1, attempts + 1):
        try:
            t0 = time.time()
            pid = submit(s, wf)
            print("已提交：%s（第 %d/%d 次）" % (pid[:8], i, attempts))
            rec, _, peak = poll(s, pid, args.timeout, cfg.get("poll_interval", 1.5))
            blobs = fetch(s, rec, meta.get("save_labels"))
            elapsed = time.time() - t0
            break
        except BridgeError as e:
            last_err = e
            retryable = e.code in (EXIT["SUBMIT"], EXIT["NOIMAGE"]) or (
                e.code == EXIT["TIMEOUT"] and i < attempts)
            print("[!] 第 %d 次失败：%s %s" % (i, e.msg, ("— " + e.detail[:300]) if e.detail else ""))
            if not retryable or i == attempts:
                return e.code
            time.sleep(2 * i)
        except requests.RequestException as e:
            last_err = e
            print("[!] 第 %d 次网络异常：%r" % (i, e))
            if i == attempts:
                return EXIT["SUBMIT"]
            time.sleep(2 * i)
    else:
        return EXIT["SUBMIT"] if last_err is None else EXIT["SUBMIT"]

    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    topic = safe_name(args.name or (os.path.splitext(os.path.basename(args.image))[0]
                                    if args.image else "AI"), cfg["chain"])
    saved = []
    tier_tag = TIERS[args._tier]["suffix"] if getattr(args, "_tier", None) else ""
    for i, (tag, data) in enumerate(blobs):
        if len(blobs) == 1:
            suffix = ""
        else:
            suffix = "__%s" % (tag or i)
        dst = os.path.join(out_dir, "%s_%s_%s%s%s_AI方案稿.png"
                           % (topic, cfg["chain"], stamp, suffix, tier_tag))
        with open(dst, "wb") as f:
            f.write(data)
        saved.append(dst)

    model = cfg.get("model") or ""
    for node in wf.values():
        if node.get("class_type") == "CheckpointLoaderSimple":
            model = node["inputs"].get("ckpt_name", model)
            break
    cn = []
    for key, node in wf.items():
        if node.get("class_type") == "ControlNetApplyAdvanced":
            cn.append({"node": key,
                       "strength": node["inputs"].get("strength"),
                       "start_percent": node["inputs"].get("start_percent"),
                       "end_percent": node["inputs"].get("end_percent")})
    cn_names = [v["inputs"].get("control_net_name") for v in wf.values()
                if v.get("class_type") == "ControlNetLoader"]

    meta_out = {
        "skill": cfg["skill"],
        "chain": cfg["chain"],
        "api": cfg["api"],
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prompt_id": pid,
        "seed": seed,
        "model": model,
        "controlnet": cn_names,
        "controlnet_apply": cn,
        "params": {k: v for k, v in applied.items() if k not in ("prompt", "image_size")},
        "intent": intent,
        "prompt": applied.get("prompt"),
        "negative": wf[meta["node_map"]["negative"]["node"]]["inputs"].get("text"),
        "source_image": os.path.abspath(args.image) if args.image else None,
        "source_sha256": sha256(args.image) if args.image and os.path.isfile(args.image) else None,
        "mask_image": os.path.abspath(args.mask) if getattr(args, "mask", None) else None,
        "mask_sha256": sha256(args.mask) if getattr(args, "mask", None) and os.path.isfile(args.mask) else None,
        "source_size": applied.get("image_size"),
        "seconds": round(elapsed, 1),
        "peak_vram_gb": round(peak, 2),
        "project_scope": scope,
        "outputs": saved,
        "server": SERVER,
    }

    written = sum(len(d) for _, d in blobs)
    log_cost({
        "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "链路": cfg["chain"],
        "档位": getattr(args, "_tier", None) or "-",
        "底模": os.path.basename(model or ""),
        "步数": applied.get("steps"),
        "尺寸": applied.get("image_size"),
        "秒": round(elapsed, 1),
        "峰值显存GB": round(peak, 2),
        "产物MB": round(written / 1048576.0, 2),
        "张数": len(saved),
    })
    print("代价：%.1f 秒 · 峰值显存 %.1f GB · 落盘 %.1f MB"
          % (elapsed, peak, written / 1048576.0))
    for p in saved:
        dump_json(os.path.splitext(p)[0] + ".meta.json", meta_out)
        print("输出：%s（%.0f KB）" % (p, os.path.getsize(p) / 1024.0))
        print("元数据：%s" % (os.path.splitext(p)[0] + ".meta.json"))

    # 闸门：先判用途，再决定打哪种标。
    #   visual + 技术图种  -> 视觉稿终态（NOT_FOR_CONSTRUCTION），不可放行
    #   --gate on          -> 强制按施工图语境打标（BLOCKED_UNTIL_APPROVED）
    #   其余               -> 不打标（纯表达）
    gate_status = "未触发"
    stamped = None
    if getattr(args, "_tier", None) == "draft":
        # 草稿不登记：一次登记要写 sidecar + 使用说明两份文件，试十个方案就是 20 份垃圾
        gate_status = "草稿不登记"
        print("[G2 闸门] 草稿档不登记、不打标——定稿时再走一遍正式档")
    elif args.gate == "off":
        print("[G2 闸门] 已关闭（--gate off）")
    elif intent == "visual" and cfg.get("technical"):
        code, log = gate_init(saved[0], cfg.get("category", "通用"), args.prompt or "", "visual")
        if code == 0:
            gate_status = "NOT_FOR_CONSTRUCTION"
            stamped = read_stamped(saved[0])
            print("")
            print("[G2 闸门] 已打标：%s（视觉稿终态，不可放行、不进交付包）" % gate_status)
            print("          打标副本：%s" % (stamped or "（见同目录 __AI视觉稿 文件）"))
            print("          须知：%s" % (os.path.splitext(saved[0])[0] + "__视觉稿使用须知.md"))
            print("          要施工图：回 CAD/BIM 重绘，本工具不提供这条路径。")
        else:
            gate_status = "INIT_FAILED"
            print("[!] 闸门执行异常：%s" % (log or "")[:300])
    elif intent == "visual":
        gate_status = register_draft(saved[0], cfg, args.prompt)
        print("[G2 闸门] 提示词提到视觉稿，但 %s 属表达类，按方案表达稿登记" % cfg["label"])
    elif args.gate == "on":
        if not cfg.get("technical"):
            gate_status = "不需要（表达类）"
            print("[G2 闸门] 未触发：%s 属表达类，不作为施工依据" % cfg["label"])
        else:
            print("[!] 强制打标：本次请求不含交付语境词")
            code, log = gate_init(saved[0], cfg.get("category", "通用"), args.prompt or "", "document")
            if code == 0:
                gate_status = "BLOCKED_UNTIL_APPROVED"
                stamped = read_stamped(saved[0])
                print("")
                print("[G2 闸门] 已打标：%s" % gate_status)
                print("          打标副本：%s" % (stamped or "（见同目录 __AI模拟稿_需专业校核 文件）"))
                print("          清单：%s" % (os.path.splitext(saved[0])[0] + "__审核清单.md"))
                print("          放行：python gate.py approve --file \"%s\" --by \"校核人\"" % saved[0])
            else:
                gate_status = "INIT_FAILED"
                print("[!] 闸门执行异常：%s" % (log or "")[:300])
    else:
        # 纯表达：不打警示条，但必须登记。没有记录的文件，交付包里既分不清
        # 是「AI 方案稿」还是「来路不明的图」，也只能一律拒收。
        gate_status = register_draft(saved[0], cfg, args.prompt)

    print("---")
    print("SKILL=%s" % cfg["skill"])
    print("CHAIN=%s" % cfg["chain"])
    print("INTENT=%s" % intent)
    print("FILES=%s" % "|".join(saved))
    print("META=%s" % "|".join(os.path.splitext(p)[0] + ".meta.json" for p in saved))
    print("SECONDS=%.1f" % elapsed)
    print("SEED=%d" % seed)
    print("GATE=%s" % gate_status)
    if stamped:
        print("STAMPED=%s" % stamped)
    if args.json:
        print(json.dumps({"skill": cfg["skill"], "chain": cfg["chain"], "files": saved,
                          "seed": seed, "seconds": round(elapsed, 1),
                          "gate": gate_status, "meta": meta_out}, ensure_ascii=False))
    return EXIT["OK"]


def main(config_path=None, argv=None):
    code = assert_process_policy()
    if code != EXIT["OK"]:
        return code

    ap = argparse.ArgumentParser(description="ComfyUI 子链桥接")
    ap.add_argument("--config", default=config_path, help="链配置（assets/chain.json）")
    ap.add_argument("-p", "--prompt", help="一句话中文/英文需求")
    ap.add_argument("-i", "--image", help="底图")
    ap.add_argument("--mask", help="遮罩图（白=要改的区域），仅局部改图链使用")
    ap.add_argument("--box", help="矩形遮罩 x1,y1,x2,y2（0-100 百分比），免手画")
    ap.add_argument("-s", "--seed", type=int)
    ap.add_argument("--denoise", type=float)
    ap.add_argument("--steps", type=int)
    ap.add_argument("--cfg", type=float, help="CFG（turbo 类模型约 1.8，常规 5-7）")
    ap.add_argument("--model", "--ckpt", dest="model",
                    help="换底模：别名 juggernaut/realvis/turbo/sdxl，或直接给 .safetensors 全名")
    ap.add_argument("--angle", help="多角度链的视角：front/side/back/aerial/top/eye，也可直接给词")
    ap.add_argument("--style", help="风格参考图（仅 ipadapter 链使用）")
    ap.add_argument("--style-weight", type=float, dest="style_weight",
                    help="风格参考强度，默认 0.65；越低越像底图，越高越像参考")
    ap.add_argument("-n", "--negative")
    ap.add_argument("--name", help="产物名前缀（默认取底图文件名）")
    ap.add_argument("--project", help="项目目录名或完整路径（默认从底图反推）")
    ap.add_argument("--out", help="直接指定输出目录（覆盖项目判定）")
    ap.add_argument("--gate", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--tier", choices=["draft", "review", "final"],
                    help="出图档位：draft 试方案 / review 汇报稿 / final 定稿（用户只说用途，档位由助手定）")
    ap.add_argument("--draft", action="store_true", help="= --tier draft")
    ap.add_argument("--sweep", action="store_true", help="盘点中间件（默认只报不删）")
    ap.add_argument("--apply", action="store_true", help="配合 --sweep 真删")
    ap.add_argument("--days", type=int, default=7, help="--sweep 的年龄线，默认 7 天")
    ap.add_argument("--retries", type=int, default=None)
    ap.add_argument("--timeout", type=int, default=None)
    ap.add_argument("--check", action="store_true", help="只做预检")
    ap.add_argument("--health", action="store_true", help="只体检服务（在线/显存/队列），不需要链配置")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.health:
        return cmd_health()
    if args.sweep:
        return cmd_sweep(args.days, args.apply)

    cfg_file = args.config
    if not cfg_file or not os.path.isfile(cfg_file):
        print("[X] 缺少链配置：%s" % cfg_file)
        return EXIT["CONFIG"]
    cfg = load_json(cfg_file)
    cfg.setdefault("skill", os.path.basename(os.path.dirname(os.path.dirname(
        os.path.abspath(cfg_file)))))
    if args.retries is None:
        args.retries = cfg.get("retries", 2)
    if args.timeout is None:
        args.timeout = cfg.get("timeout", 900)

    try:
        if args.check:
            return cmd_check(cfg, args)
        if cfg.get("needs_image", True) and not args.image and not args.dry_run:
            print("[X] %s 需要底图，请加 -i" % cfg["label"])
            return EXIT["CONFIG"]
        if (cfg.get("needs_mask") and not getattr(args, "mask", None)
                and not getattr(args, "box", None) and not args.dry_run):
            print("[X] %s 需要遮罩：--mask 给遮罩图，或 --box x1,y1,x2,y2 框一块" % cfg["label"])
            return EXIT["CONFIG"]
        if cfg.get("needs_style") and not getattr(args, "style", None) and not args.dry_run:
            print("[X] %s 需要风格参考图，请加 --style（例如甲方给的那张参考图）" % cfg["label"])
            return EXIT["CONFIG"]
        args.session = session()
        return cmd_run(cfg, args)
    except BridgeError as e:
        print("[X] %s%s" % (e.msg, ("\n" + e.detail) if e.detail else ""))
        return e.code


# ==== 进程策略自检边界：以下内容不参与源码扫描 ====
# 硬契约（用户定，不许改）：
#   ComfyUI 由用户手动启动并常驻，本工具只提交任务，绝不启停进程。
#   理由：助手环境若拉起常驻服务，会长期占住显存，而且不受用户控制。
# 实现方式：每次运行先扫自己上面这段源码，出现启停进程的写法就直接拒绝执行。
# 下面这些词用拼接写，避免扫描器匹配到自己。
_LAUNCH = ("Po" + "pen", "start" + "file", "os.sys" + "tem", "task" + "kill",
           "shell=" + "True", "CreatePro" + "cess")
# 子进程只允许调 gate.py 之类的本地脚本；出现 bat / ComfyUI 的 main.py 就是想起服务
_TARGETS = (".b" + "at", "main.py")
_BOUNDARY = "==== 进程策略自检边界"


def policy_scan(src_path=None):
    """扫源码：出现拉起/杀死进程的写法就返回命中行。"""
    src_path = src_path or os.path.abspath(__file__)
    with io.open(src_path, encoding="utf-8") as f:
        lines = f.readlines()
    cut = len(lines)
    for i, line in enumerate(lines):
        if _BOUNDARY in line:
            cut = i
            break
    hits = []
    for i, line in enumerate(lines[:cut], 1):
        code = line.split("#", 1)[0]
        if not code.strip():
            continue
        if any(t in code for t in _LAUNCH):
            hits.append((i, line.strip()[:100]))
        elif "subprocess." in code and any(t in code for t in _TARGETS):
            hits.append((i, line.strip()[:100]))
    return hits


def assert_process_policy(src_path=None):
    """守住显存：宁可这次不出图，也不许工具自己去开服务。"""
    hits = policy_scan(src_path)
    if not hits:
        return EXIT["OK"]
    print("[X] 进程策略检查未通过：本工具不允许启动或停止 ComfyUI。")
    for i, code in hits:
        print("    第 %d 行：%s" % (i, code))
    print("    ComfyUI 由你手动启动并常驻，助手只提交任务——常驻进程会一直占着显存。")
    return EXIT["REFUSE"]


if __name__ == "__main__":
    sys.exit(main())
