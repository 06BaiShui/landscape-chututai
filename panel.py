# -*- coding: utf-8 -*-
"""本机出图台：拖一张底图，选一条链，出一张图；另挂编辑台与展板台两个工作台。

设计原则
--------
1. 不重造轮子：全部 24 条链统一走共享引擎 comfy_bridge.py（--config 挂
   chains/ 下的薄链配置，工作流模板在 D:\\ComfyUI\\landscape_toolbox\\workflows\\api），
   超分走独立的 upscale.py。本文件只做「网页表单 -> 命令行 -> 回传结果」的搬运。
2. 只用标准库：这台机器不需要为一个界面再装 Flask。
3. 不碰 ComfyUI 进程：8188 没开就报「请先双击启动 ComfyUI」，不自己拉、也不关。
4. 只监听 127.0.0.1：局域网里别的机器看不到这个页面。
"""

import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import library  # 成果库：分类视图 / 直删 / 归档 / 撤销 / 缩略图

HERE = os.path.dirname(os.path.abspath(__file__))
# 用启动面板的同一个解释器跑子任务（bat / python 直启都成立），不再写死本机 venv。
PYEXE = os.environ.get("CHUTAI_PYTHON") or sys.executable
SKILLS = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")
# 引擎优先用仓库自带的 bridge/（GitHub 分发版），没有再退回 WorkBuddy skill 目录。
_LOCAL_BRIDGE = os.path.join(HERE, "bridge", "comfy_bridge.py")
BRIDGE = (_LOCAL_BRIDGE if os.path.isfile(_LOCAL_BRIDGE)
          else os.path.join(SKILLS, "comfy-render", "scripts", "comfy_bridge.py"))
UPSCALE = os.path.join(HERE, "upscale.py")
# 工作流模板：仓库自带 workflows/api/ 优先，其次本机 landscape_toolbox。
_LOCAL_TOOLBOX = os.path.join(HERE, "workflows", "api")
TOOLBOX = (_LOCAL_TOOLBOX if os.path.isdir(_LOCAL_TOOLBOX)
           else r"D:\ComfyUI\landscape_toolbox\workflows\api")
# bridge 用 COMFY_TOOLBOX 找模板（它要的是 toolbox 根目录，不是 api 子目录）
if os.path.isdir(_LOCAL_TOOLBOX):
    os.environ.setdefault("COMFY_TOOLBOX", HERE)
# 项目根：环境变量 > 本机 D:\workplace\设计项目 > 仓库内 项目/
_DEF_PROJECTS = r"D:\workplace\设计项目"
PROJECTS_ROOT = (os.environ.get("CHUTAI_PROJECTS")
                 if os.environ.get("CHUTAI_PROJECTS")
                 else (_DEF_PROJECTS if os.path.isdir(_DEF_PROJECTS)
                       else os.path.join(HERE, "项目")))

# 全部链统一走共享引擎（comfy_bridge --config 挂链模板），
# 模板在 D:\ComfyUI\landscape_toolbox\workflows\api\*_api.json，参数注入看 _node_map.json。
# 需要额外输入的链：ipadapter=参考图、inpaint=遮罩框、multiangle=视角、board_hero=无需底图。
CHAINS = {
    "perspective":       (os.path.join(HERE, "chains", "perspective.json"),       "白模 → 效果图"),
    "aerial":            (os.path.join(HERE, "chains", "aerial.json"),            "总平转鸟瞰（实验）"),
    "section_perspective": (os.path.join(HERE, "chains", "section_perspective.json"), "剖透视（剖面填色）"),
    "axon":              (os.path.join(HERE, "chains", "axon.json"),              "轴测表现"),
    "facade":            (os.path.join(HERE, "chains", "facade.json"),            "建筑立面表现"),
    "interior":          (os.path.join(HERE, "chains", "interior.json"),          "室内空间效果"),
    "massing":           (os.path.join(HERE, "chains", "massing.json"),           "体块推敲"),
    "sketch":            (os.path.join(HERE, "chains", "sketch.json"),            "草图 / 白模渲染"),
    "planting":          (os.path.join(HERE, "chains", "planting.json"),          "种植意向图"),
    "renov":             (os.path.join(HERE, "chains", "renov.json"),             "改造 / 现状更新"),
    "relight":           (os.path.join(HERE, "chains", "relight.json"),           "日夜 / 季节 / 氛围"),
    "multiangle":        (os.path.join(HERE, "chains", "multiangle.json"),        "多角度（换视角重画）"),
    "color_plan":        (os.path.join(HERE, "chains", "color_plan.json"),        "彩平（CAD 线稿上色）"),
    "plan_arch":         (os.path.join(HERE, "chains", "plan_arch.json"),         "建筑总平彩图"),
    "analysis_diagram":  (os.path.join(HERE, "chains", "analysis_diagram.json"),  "分析图（图解风格）"),
    "sitediag":          (os.path.join(HERE, "chains", "sitediag.json"),          "现状分析图"),
    "style":             (os.path.join(HERE, "chains", "style.json"),             "分析图 / 插画风"),
    "exploded":          (os.path.join(HERE, "chains", "exploded.json"),          "爆炸图 / 分层轴测"),
    "lora":              (os.path.join(HERE, "chains", "lora_siheyuan.json"),  "中式院落 LoRA"),
    "watercolor":        (os.path.join(HERE, "chains", "watercolor.json"),     "水彩风格化"),
    "outpaint":          (os.path.join(HERE, "chains", "outpaint.json"),       "一键扩图（四边外扩）"),
    "interior_t2i":      (os.path.join(HERE, "chains", "interior_t2i.json"),   "文生图·室内场景"),
    "nature_t2i":        (os.path.join(HERE, "chains", "nature_t2i.json"),     "文生图·自然风景"),
    "ipadapter":         (os.path.join(HERE, "chains", "ipadapter.json"),         "风格参考（抄参考图风格）"),
    "material":          (os.path.join(HERE, "chains", "material.json"),          "材质替换"),
    "inpaint":           (os.path.join(HERE, "chains", "inpaint.json"),           "局部改图（框选重绘）"),
    "enhance":           (os.path.join(HERE, "chains", "enhance.json"),           "洗图（原尺寸提细节）"),
    "board_hero":        (os.path.join(HERE, "chains", "board_hero.json"),        "展板主图（纯文生图）"),
    "sansan_plan":       (os.path.join(HERE, "chains", "sansan_plan.json"),       "平面方案上色（叁十三 LoRA）"),
    "concept_sketch":    (os.path.join(HERE, "chains", "concept_sketch.json"),    "概念草图（一句话→草图底稿）"),
    "concept_base":      (os.path.join(HERE, "chains", "concept_base.json"),      "概念底稿（线稿+12段式提示词）"),
    "plan_to_mass":      (os.path.join(HERE, "chains", "plan_to_mass.json"),      "平面图→轴测白模（自动 45° 投影）"),
}
# 这些链不吃底模切换 / 档位以外的特殊处理；upscale 是纯超分，走自己的脚本
NEEDS_STYLE = {"ipadapter"}
NEEDS_BOX = {"inpaint"}
NEEDS_ANGLE = {"multiangle"}
NO_IMAGE = {"board_hero", "interior_t2i", "nature_t2i", "concept_sketch"}
MODELS = {
    "juggernaut": "Juggernaut-XL · 默认（均衡）",
    "turbo": "DreamShaperXL Turbo · 8 步 / 约 17 s（快速定构图）",
    "realvis": "RealVisXL V5 · 写实质感",
    "sdxl": "SDXL 1.0 原生基线",
}
UPLOAD_DIR = os.path.join(HERE, "上传")
PORT = 8760
COMFY = "http://127.0.0.1:8188"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
BOARD_DIRNAME = os.path.join("出图", "展板")   # 项目内展板成果，不含「AI方案稿」→ 不进成果库统计
BOARD_FALLBACK = os.path.join(PROJECTS_ROOT, "_展板")  # 没选项目时的公共展板目录

# 批注 / 展板用的常用字体。installed 由 /api/fonts 扫系统字体目录实测，
# 没装的字体会被前端标灰但仍可选（浏览器会回退到系统默认，不会乱码）。
FONT_CANDIDATES = [
    ("微软雅黑", '"Microsoft YaHei",sans-serif', ["msyh.ttc", "msyh.ttf", "msyhbd.ttc"]),
    ("微软雅黑 Light", '"Microsoft YaHei Light","Microsoft YaHei",sans-serif', ["msyhl.ttc"]),
    ("黑体", 'SimHei,"Microsoft YaHei",sans-serif', ["simhei.ttf"]),
    ("宋体", 'SimSun,serif', ["simsun.ttc"]),
    ("仿宋", 'FangSong,SimSun,serif', ["simfang.ttf"]),
    ("楷体", 'KaiTi,STKaiti,serif', ["simkai.ttf"]),
    ("等线", 'DengXian,"Microsoft YaHei",sans-serif', ["Deng.ttf", "Dengb.ttf"]),
    ("思源黑体", '"Source Han Sans SC","Noto Sans CJK SC",sans-serif',
     ["SourceHanSansSC-Regular.otf", "NotoSansCJKsc-Regular.otf"]),
    ("思源宋体", '"Source Han Serif SC","Noto Serif CJK SC",serif',
     ["SourceHanSerifSC-Regular.otf", "NotoSerifCJKsc-Regular.otf"]),
    ("方正小标宋简体", '"FZXiaoBiaoSong-B05S","FZXiaoBiaoSong-B05",SimSun,serif',
     ["FZXBSK.TTF", "fzxbssjw.ttf"]),
    ("方正兰亭黑", '"FZLanTingHeiS-R-GB","Microsoft YaHei",sans-serif', ["FZLTHJW.TTF"]),
    ("Arial", 'Arial,Helvetica,sans-serif', ["arial.ttf"]),
    ("Arial Black", '"Arial Black",Arial,sans-serif', ["ariblk.ttf"]),
    ("Calibri", 'Calibri,"Microsoft YaHei",sans-serif', ["calibri.ttf"]),
    ("Times New Roman", '"Times New Roman",Times,serif', ["times.ttf"]),
    ("Georgia", 'Georgia,"Times New Roman",serif', ["georgia.ttf"]),
    ("Verdana", 'Verdana,Geneva,sans-serif', ["verdana.ttf"]),
    ("Impact", 'Impact,"Arial Black",sans-serif', ["impact.ttf"]),
    ("Consolas", 'Consolas,"Courier New",monospace', ["consola.ttf"]),
    ("Trebuchet MS", '"Trebuchet MS",Arial,sans-serif', ["trebuc.ttf"]),
]
# Photoshop ExtendScript 用的是 PostScript 字体名，不是 CSS family。
# 导出 .jsx 时写这个；PS 找不到会被脚本里的 try/catch 兜住，不至于整个脚本崩掉。
PS_NAMES = {
    "微软雅黑": "MicrosoftYaHei", "微软雅黑 Light": "MicrosoftYaHeiLight",
    "黑体": "SimHei", "宋体": "SimSun", "仿宋": "FangSong", "楷体": "KaiTi",
    "等线": "DengXian", "Arial": "Arial", "Arial Black": "Arial-BoldMT",
    "Calibri": "Calibri", "Times New Roman": "TimesNewRomanPSMT",
    "Georgia": "Georgia", "Verdana": "Verdana", "Impact": "Impact",
    "Consolas": "Consolas", "Trebuchet MS": "TrebuchetMS",
}

JOBS = {}
JOB_SEQ = [0]
LOCK = threading.Lock()
RUNNING = [None]          # 同一时刻只跑一张：8 GB 显存扛不住并发


# ---------------------------------------------------------------- 状态探测

# 直连 127.0.0.1，绝不走系统代理：本机开了代理（如 127.0.0.1:7260）时，
# urlopen 默认继承 HTTP_PROXY，访问 8188 会被代理转一圈，ComfyUI 没开时
# 每次都要等代理超时约 2 秒才报 502，/api/state 因此常年卡 2 秒。
_COMFY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_COMFY_LOCK = threading.Lock()
_COMFY_CACHE = {"t": 0.0, "v": {"online": False, "vram_free": 0,
                                "vram_total": 0, "queue": 0}}


def comfy_state():
    """探测 ComfyUI；结果缓存 3 秒，主页面每 8 秒轮询、成果库打开也要用，
    不缓存的话探测线程会一直挂在代理超时上。"""
    now = time.time()
    with _COMFY_LOCK:
        if now - _COMFY_CACHE["t"] < 3:
            return _COMFY_CACHE["v"]
    try:
        with _COMFY_OPENER.open(COMFY + "/system_stats", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
        dev = (d.get("devices") or [{}])[0]
        v = {"online": True,
             "vram_free": round((dev.get("vram_free") or 0) / 2.0 ** 30, 1),
             "vram_total": round((dev.get("vram_total") or 0) / 2.0 ** 30, 1),
             "queue": d.get("exec_info", {}).get("queue_remaining", 0)}
    except Exception:
        v = {"online": False, "vram_free": 0, "vram_total": 0, "queue": 0}
    with _COMFY_LOCK:
        _COMFY_CACHE["t"] = now
        _COMFY_CACHE["v"] = v
    return v


def scan_projects():
    """列项目，按最近改动排前面——刚做过的项目一定在顶上，不用翻。"""
    out = []
    if not os.path.isdir(PROJECTS_ROOT):
        return out
    skip = ("ps源文件", "skp备份", "其他", "备份", "backup")
    for cat in sorted(os.listdir(PROJECTS_ROOT)):
        cdir = os.path.join(PROJECTS_ROOT, cat)
        if not os.path.isdir(cdir) or cat.startswith(("_", ".")):
            continue
        for proj in sorted(os.listdir(cdir)):
            pdir = os.path.join(cdir, proj)
            if not os.path.isdir(pdir) or proj.startswith(("_", ".")):
                continue
            if proj.lower() in skip or "备份" in proj or "备份" in cat:
                continue
            try:
                mt = os.path.getmtime(pdir)
            except OSError:
                mt = 0
            out.append({"cat": cat, "name": proj, "path": pdir, "mtime": mt})
    out.sort(key=lambda x: -x["mtime"])
    for x in out:
        x.pop("mtime", None)
    return out


def list_images(root, depth=2):
    """列项目里的可用底图：底图/、su/shots/ 优先，其次浅层扫。"""
    hits = []
    if not os.path.isdir(root):
        return hits
    for sub in ("底图", os.path.join("su", "shots"), "图纸", ""):
        d = os.path.join(root, sub) if sub else root
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(p) and name.lower().endswith(IMG_EXT):
                hits.append({"path": p, "name": name,
                             "where": os.path.relpath(d, root) if sub else "根目录",
                             "size": os.path.getsize(p)})
    return hits


# ---------------------------------------------------------------- 出图任务

def _parse(line, job):
    m = re.search(r"输出：(.+?\.png)", line)
    if m:
        job["files"].append(m.group(1).strip())
    m = re.search(r"代价：([\d.]+) 秒 · 峰值显存 ([\d.]+) GB", line)
    if m:
        job["seconds"] = float(m.group(1))
        job["vram"] = float(m.group(2))
    m = re.search(r"预算：(\d+×\d+)", line)
    if m:
        job["size"] = m.group(1)


def worker(job):
    p = job["params"]
    chain = p["chain"]
    if chain == "upscale":
        args = [PYEXE, UPSCALE, "-i", p["image_path"]]
    else:
        cfg = CHAINS[chain][0]
        args = [PYEXE, BRIDGE, "--config", cfg]
        if p.get("image_path"):
            args += ["-i", p["image_path"]]
        if chain in NEEDS_STYLE and p.get("style_path"):
            args += ["--style", p["style_path"]]
        if chain in NEEDS_BOX and p.get("mask_path"):
            # 编辑台画的任意形状遮罩（白=重绘，黑=保留），引擎以 --mask 优先于 --box
            args += ["--mask", p["mask_path"]]
        elif chain in NEEDS_BOX and p.get("box"):
            args += ["--box", ",".join(str(round(v, 2)) for v in p["box"])]
        if chain in NEEDS_ANGLE and p.get("angle"):
            args += ["--angle", p["angle"]]
        if p.get("prompt"):
            args += ["-p", p["prompt"]]
        if p.get("tier"):
            args += ["--tier", p["tier"]]
        if p.get("denoise") is not None:
            args += ["--denoise", str(p["denoise"])]
        if p.get("seed"):
            args += ["-s", str(p["seed"])]
        if p.get("model"):
            args += ["--model", p["model"]]
    if p.get("project"):
        args += ["--project", p["project"]]

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    job["status"] = "running"
    job["cmd"] = " ".join('"%s"' % a if " " in a else a for a in args[1:])
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    cwd = os.path.dirname(p["image_path"]) if p.get("image_path") else UPLOAD_DIR
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=cwd,
                                env=env, encoding="utf-8", errors="replace", bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            job["log"].append(line)
            _parse(line, job)
            if len(job["log"]) > 400:
                del job["log"][:100]
        code = proc.wait()
        job["code"] = code
        if code == 9:
            job["status"] = "error"
            job["error"] = "ComfyUI 没在跑：请先双击桌面「启动 ComfyUI」，等控制台出现 URL 后再点生成。"
        elif code == 0 and job["files"]:
            job["status"] = "done"
        elif code == 0:
            job["status"] = "error"
            job["error"] = "跑完了但没有产物，看下面的日志。"
        else:
            job["status"] = "error"
            job["error"] = "退出码 %d，看下面的日志。" % code
    except Exception as e:
        job["status"] = "error"
        job["error"] = "启动失败：%s" % e
    finally:
        job["ended"] = time.time()
        with LOCK:
            RUNNING[0] = None


def new_job(params):
    with LOCK:
        JOB_SEQ[0] += 1
        jid = "%d" % JOB_SEQ[0]
        job = {"id": jid, "status": "queued", "log": [], "files": [], "params": params,
               "started": time.time(), "ended": None, "error": None,
               "seconds": None, "vram": None, "size": None, "code": None}
        JOBS[jid] = job
        RUNNING[0] = jid
    threading.Thread(target=worker, args=(job,), daemon=True).start()
    return job


# ---------------------------------------------------------------- HTTP

def safe_file(path):
    """只允许读 D:\\workplace 下的文件，避免这个端口变成任意文件下载器。"""
    try:
        p = os.path.abspath(path)
        return p if p.lower().startswith(os.path.abspath(r"D:\workplace").lower()) and os.path.isfile(p) else None
    except Exception:
        return None


def make_iso_plan(src):
    """正投影平面线稿 → 45° 等轴测菱形线稿（PIL，ComfyUI 环境自带 Pillow）。

    实测 2026-11-14：不转的话 canny 控制网把视角锁死在正俯视，只会把线稿描一遍；
    转成菱形后控制网引导的就是轴测轮廓，能同时保住「等轴测视角 + 平面布局」。
    """
    from PIL import Image
    base = os.path.basename(src)
    if base.startswith("_轴测_"):
        return src
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    dst = os.path.join(UPLOAD_DIR, "_轴测_" + base)
    if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    im = Image.open(src).convert("RGB")
    r = im.rotate(45, expand=True, resample=Image.BICUBIC, fillcolor="white")
    r = r.resize((r.width, int(r.height * 0.60)), Image.LANCZOS)
    r.save(dst)
    return dst


# ---------------------------------------------------------------- 编辑 / 展板 辅助

def scan_fonts():
    """实测本机装了哪些常用字体，前端下拉只显示装了的（避免选了却回退成宋体）。"""
    dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
            os.path.join(os.path.expanduser("~"), "AppData", "Local",
                         "Microsoft", "Windows", "Fonts")]
    have = set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            for f in os.listdir(d):
                have.add(f.lower())
        except OSError:
            pass
    fonts = [{"name": n, "family": fam, "ps": PS_NAMES.get(n) or
              fam.split(",")[0].strip().strip('"').replace(" ", ""),
              "installed": any(x.lower() in have for x in files)}
             for n, fam, files in FONT_CANDIDATES]
    return {"fonts": fonts, "installed": sum(1 for f in fonts if f["installed"])}


def board_dir(project=None):
    """展板成果目录：选了项目 → <项目>/出图/展板/，否则 → 设计项目/_展板/<日期>/。

    刻意不放进「出图/AI方案稿」：成果库只认 AI方案稿 / _未归档AI出图，
    排版稿混进去会被当成 AI 出图统计，也可能被误删。
    """
    if project:
        ap = os.path.abspath(project)
        root = os.path.abspath(PROJECTS_ROOT)
        if ap.lower().startswith(root.lower() + os.sep) and os.path.isdir(ap):
            return os.path.join(ap, BOARD_DIRNAME)
    return os.path.join(BOARD_FALLBACK, time.strftime("%Y%m%d"))


def _clean_name(name, fallback="未命名"):
    stem = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", os.path.splitext(os.path.basename(name or ""))[0])
    return (stem.strip().strip(".") or fallback)[:60]


def save_b64(b64, name, directory, prefix=""):
    """把前端 canvas / 上传的 data URL 落到本机，返回绝对路径。"""
    import base64 as _b64
    os.makedirs(directory, exist_ok=True)
    dst = os.path.join(directory, "%s%s_%s.png" %
                       (prefix, _clean_name(name), time.strftime("%Y%m%d_%H%M%S")))
    if os.path.exists(dst):                      # 同秒重名不覆盖
        dst = dst[:-4] + "_%d.png" % (time.time_ns() % 10000)
    with open(dst, "wb") as f:
        f.write(_b64.b64decode(b64.split(",", 1)[-1]))
    return dst


def board_list():
    import glob
    pats = [os.path.join(PROJECTS_ROOT, "*", "*", BOARD_DIRNAME, "*.layout.json"),
            os.path.join(BOARD_FALLBACK, "**", "*.layout.json")]
    out = []
    seen = set()
    for pat in pats:
        for p in glob.glob(pat, recursive=True):
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            seen.add(ap)
            png = ap[:-len(".layout.json")] + ".png"
            proj = ""
            try:
                rel = os.path.relpath(ap, PROJECTS_ROOT).split(os.sep)
                if len(rel) > 2 and not rel[0].startswith("_"):
                    proj = rel[1]
            except Exception:
                pass
            out.append({"name": os.path.basename(ap)[:-len(".layout.json")],
                        "layout": ap, "png": png if os.path.isfile(png) else None,
                        "project": proj, "mtime": os.path.getmtime(ap)})
    out.sort(key=lambda x: -x["mtime"])
    for x in out:
        x.pop("mtime", None)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "ChuTuTai/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    # -- helpers
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    # -- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            p = os.path.join(HERE, "ui.html")
            if not os.path.isfile(p):
                return self._send(500, "缺少 ui.html")
            with open(p, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path in ("/editor.html", "/board.html"):
            p = os.path.join(HERE, u.path.lstrip("/"))
            if not os.path.isfile(p):
                return self._send(500, "缺少 %s" % u.path.lstrip("/"))
            with open(p, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/fonts":
            return self._json(scan_fonts())
        if u.path == "/api/board/list":
            return self._json({"boards": board_list()})
        if u.path == "/api/board/load":
            p = safe_file((q.get("path") or [""])[0])
            if not p or not p.endswith(".layout.json"):
                return self._json({"error": "布局文件路径不对"}, 400)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return self._json({"layout": json.load(f), "path": p})
            except Exception as e:
                return self._json({"error": "读布局失败：%s" % e}, 500)
        if u.path == "/api/state":
            return self._json({"comfy": comfy_state(), "projects": scan_projects(),
                               "models": MODELS, "busy": RUNNING[0], "port": PORT,
                               "categories": library.CATEGORIES,
                               "chain_labels": library.CHAIN_LABELS,
                               "unfiled": library.UNFILED})
        if u.path == "/api/outputs":
            return self._json(library.scan((q.get("project") or [""])[0] or None,
                                           (q.get("all") or [""])[0] == "1"))
        if u.path == "/api/thumb":
            data = library.thumbnail((q.get("path") or [""])[0])
            if not data:
                return self._send(404, "not found")
            return self._send(200, data, "image/jpeg")
        if u.path == "/api/images":
            d = (q.get("dir") or [""])[0]
            if not d or not os.path.isdir(d):
                return self._json({"images": []})
            return self._json({"images": list_images(d)})
        if u.path == "/api/job":
            jid = (q.get("id") or [""])[0]
            j = JOBS.get(jid)
            if not j:
                return self._json({"error": "没有这个任务"}, 404)
            return self._json({k: j[k] for k in
                               ("id", "status", "log", "files", "error", "seconds",
                                "vram", "size", "code")} |
                              {"elapsed": round((j["ended"] or time.time()) - j["started"], 1)})
        if u.path == "/api/file":
            p = safe_file((q.get("path") or [""])[0])
            if not p:
                return self._send(404, "not found")
            ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
            with open(p, "rb") as f:
                return self._send(200, f.read(), ctype)
        if u.path == "/api/reveal":
            p = safe_file((q.get("path") or [""])[0])
            if not p:
                return self._json({"ok": False}, 404)
            subprocess.Popen(["explorer", "/select,", p])
            return self._json({"ok": True})
        if u.path == "/api/open_dir":
            # 展板台「打开文件夹」给的是目录，/api/reveal 只收文件
            p = os.path.abspath((q.get("path") or [""])[0])
            if not p.lower().startswith(os.path.abspath(r"D:\workplace").lower()) or not os.path.isdir(p):
                return self._json({"ok": False}, 404)
            subprocess.Popen(["explorer", p])
            return self._json({"ok": True})
        return self._send(404, "not found")

    # -- POST
    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return self._json({"error": "请求不是合法 JSON"}, 400)

        if u.path == "/api/upload_file":
            # 编辑台 / 展板台把本地拖进来的图或遮罩先落盘，后续一律用绝对路径引用，
            # 避免每改一轮都重复上传几 MB 的 base64。
            b64 = body.get("b64") or body.get("image_b64")
            if not b64:
                return self._json({"error": "没有图像数据"}, 400)
            kind = "遮罩" if body.get("kind") == "mask" else "底图"
            try:
                p = save_b64(b64, body.get("name") or kind, UPLOAD_DIR, "_%s_" % kind)
                return self._json({"path": p, "name": os.path.basename(p)})
            except Exception as e:
                return self._json({"error": "保存失败：%s" % e}, 500)

        if u.path == "/api/save_file":
            # 批注导出 / 展板成稿：PNG 落 <项目>/出图/展板/，展板另存 .layout.json 可回打开继续排
            b64 = body.get("b64")
            if not b64:
                return self._json({"error": "没有图像数据"}, 400)
            kind = body.get("kind") or "annotate"
            label = "展板" if kind == "board" else "批注"
            d = board_dir(body.get("project"))
            try:
                png = save_b64(b64, body.get("name") or label, d, label + "_")
                layout_path = None
                if kind == "board" and body.get("layout") is not None:
                    layout_path = png[:-4] + ".layout.json"
                    with open(layout_path, "w", encoding="utf-8") as f:
                        json.dump(body["layout"], f, ensure_ascii=False, indent=1)
                return self._json({"ok": True, "path": png, "layout": layout_path,
                                   "dir": d, "size": os.path.getsize(png)})
            except Exception as e:
                return self._json({"error": "保存失败：%s" % e}, 500)

        if u.path == "/api/save_text":
            # 展板台导出的 Photoshop ExtendScript（.jsx）。UTF-8 BOM + CRLF：
            # PS 的 ExtendScript 引擎读中文脚本靠 BOM 判断编码，不写 BOM 会乱码。
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return self._json({"error": "没有脚本内容"}, 400)
            ext = body.get("ext") or ".jsx"
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
                ext = ".jsx"
            d = board_dir(body.get("project"))
            try:
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, "展板脚本_%s_%s%s" %
                                 (_clean_name(body.get("name") or "展板"),
                                  time.strftime("%Y%m%d_%H%M%S"), ext))
                if os.path.exists(p):
                    p = p[:-len(ext)] + "_%d%s" % (time.time_ns() % 10000, ext)
                with open(p, "w", encoding="utf-8-sig", newline="\r\n") as f:
                    f.write(text)
                return self._json({"ok": True, "path": p, "dir": d,
                                   "size": os.path.getsize(p)})
            except Exception as e:
                return self._json({"error": "保存脚本失败：%s" % e}, 500)

        if u.path == "/api/render":
            if RUNNING[0]:
                return self._json({"error": "正在出图，等这张跑完再点"}, 409)
            chain = body.get("chain")
            if chain not in CHAINS and chain != "upscale":
                return self._json({"error": "未知链路：%s" % chain}, 400)

            image_path = None
            if body.get("image_path"):
                image_path = safe_file(body["image_path"])
                if not image_path:
                    return self._json({"error": "底图路径不在 D:\\workplace 下"}, 400)
            elif body.get("image_b64"):
                image_path = self._save_upload(body.get("filename") or "底图.png", body["image_b64"])
            elif chain not in NO_IMAGE:
                return self._json({"error": "没有底图"}, 400)

            # 平面图→轴测白模：先把正投影平面做 45° 轴测投影，否则控制网锁死正俯视
            if chain == "plan_to_mass" and image_path:
                image_path = make_iso_plan(image_path)

            style_path = None
            if chain in NEEDS_STYLE:
                if body.get("style_path"):
                    style_path = safe_file(body["style_path"])
                    if not style_path:
                        return self._json({"error": "参考图路径不在 D:\\workplace 下"}, 400)
                elif body.get("style_b64"):
                    style_path = self._save_upload(body.get("style_filename") or "参考图.png",
                                                   body["style_b64"])
                else:
                    return self._json({"error": "风格参考链要先给一张参考图（右侧参考图区）"}, 400)
            mask_path = None
            if chain in NEEDS_BOX:
                if body.get("mask_b64"):
                    mask_path = save_b64(body["mask_b64"],
                                         body.get("filename") or "遮罩", UPLOAD_DIR, "_遮罩_")
                elif body.get("mask_path"):
                    mask_path = safe_file(body["mask_path"])
                    if not mask_path:
                        return self._json({"error": "遮罩路径不在 D:\\workplace 下"}, 400)
                elif not body.get("box"):
                    return self._json({"error": "局部改图要先圈一块区域（画笔 / 形状 / 套索）"}, 400)

            job = new_job({"chain": chain, "image_path": image_path,
                           "style_path": style_path, "mask_path": mask_path,
                           "box": body.get("box"),
                           "angle": (body.get("angle") or "").strip() or None,
                           "prompt": (body.get("prompt") or "").strip(),
                           "tier": body.get("tier") or "review",
                           "denoise": body.get("denoise"),
                           "seed": body.get("seed"),
                           "model": body.get("model") or None,
                           "project": body.get("project") or None})
            return self._json({"job": job["id"]})

        if u.path in ("/api/delete", "/api/organize", "/api/undo"):
            if RUNNING[0]:
                return self._json({"error": "正在出图，等这张跑完再整理"}, 409)
            project = body.get("project") or None
            allp = bool(body.get("all"))
            try:
                if u.path == "/api/delete":
                    return self._json(library.delete(body.get("paths") or []))
                if u.path == "/api/organize":
                    return self._json(library.organize(project, allp, body.get("paths")))
                return self._json(library.undo_organize(project, allp))
            except Exception as e:
                return self._json({"error": "整理失败：%s" % e}, 500)
        return self._send(404, "not found")

    def _save_upload(self, name, b64):
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(name))
        if ext.lower() not in IMG_EXT:
            ext = ".png"
        dst = os.path.join(UPLOAD_DIR, "%s_%s%s" % (stem[:40], time.strftime("%H%M%S"), ext))
        data = b64.split(",", 1)[-1]
        with open(dst, "wb") as f:
            f.write(base64.b64decode(data))
        return dst


def main():
    if not os.path.isfile(PYEXE):
        sys.stderr.write("[X] 找不到 Python：%s\n" % PYEXE)
        return 2
    for path in (BRIDGE, UPSCALE):
        if not os.path.isfile(path):
            sys.stderr.write("[X] 找不到引擎脚本：%s\n" % path)
            return 2
    for k, (cfg, _label) in CHAINS.items():
        if not os.path.isfile(cfg):
            sys.stderr.write("[X] %s 缺少链配置：%s\n" % (k, cfg))
            return 2
    st = comfy_state()
    print("=" * 56)
    print("  出图台  http://127.0.0.1:%d" % PORT)
    print("  编辑台  http://127.0.0.1:%d/editor.html   （圈选重绘 / 批注）" % PORT)
    print("  展板台  http://127.0.0.1:%d/board.html    （多图画布排版）" % PORT)
    print("  ComfyUI %s" % ("在线 · 可用 %.1f GB" % st["vram_free"] if st["online"]
                            else "未启动（生成时会报错，先双击桌面「启动 ComfyUI」）"))
    print("  关掉这个黑窗口即退出。出图期间别关；跑完可以直接关。")
    print("=" * 56)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print("端口 %d 已被占用：大概已经开着一个出图台了，直接给你开浏览器。" % PORT)
        webbrowser.open("http://127.0.0.1:%d" % PORT)
        return 0
    if "--no-browser" not in sys.argv:          # 双击启动时自动开浏览器；自测传 --no-browser
        threading.Timer(0.8, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
