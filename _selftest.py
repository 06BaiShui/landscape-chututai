# -*- coding: utf-8 -*-
"""出图台回归自测：三条链 + 换底模 + AI 放大 + 并发保护 + 越权拦截。

用法：先跑 panel.py --no-browser，再跑本文件。
"""
import json
import time
import urllib.parse
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8760"
PROJ = r"D:\workplace\设计项目\建筑构造\2402024214-蒙春燕-廊架白模"
IMG = PROJ + r"\su\shots\01_人视.png"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return r.status, r.read(), r.headers.get("Content-Type")


def post(path, obj):
    req = urllib.request.Request(BASE + path, data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait(jid, limit=240):
    t0 = time.time()
    while time.time() - t0 < limit:
        _, raw, _ = get("/api/job?id=" + jid)
        j = json.loads(raw.decode("utf-8"))
        if j["status"] in ("done", "error"):
            return j
        time.sleep(1)
    return {"status": "timeout"}


def run(name, payload):
    code, d = post("/api/render", payload)
    if code != 200:
        print("   [%-16s] 提交失败 HTTP %d %s" % (name, code, d))
        return None
    j = wait(d["job"])
    f = j["files"][-1] if j.get("files") else "-"
    print("   [%-16s] %-6s %5s s  %s GB  %s" %
          (name, j.get("status"), j.get("seconds") or "-", j.get("vram") or "-",
           f.split("\\")[-1][:52]))
    if j.get("error"):
        print("        错误:", j["error"])
    if j.get("status") == "done" and f != "-":
        _, data, ctype = get("/api/file?path=" + urllib.parse.quote(f))
        print("        取图 %s %d bytes" % (ctype, len(data)))
    return j


print("0) 服务状态")
_, raw, _ = get("/api/state")
st = json.loads(raw.decode("utf-8"))
print("   ComfyUI:", "在线 %.1f/%.1f GB" % (st["comfy"]["vram_free"], st["comfy"]["vram_total"])
      if st["comfy"]["online"] else "离线")
print("   底模:", list((st.get("models") or {}).keys()))
print("   项目(前2):", ["%s/%s" % (p["cat"], p["name"]) for p in st["projects"][:2]])

print("1) 并发保护（跑第一张的同时再提交，应 409）")
code, first = post("/api/render", {"chain": "perspective", "image_path": IMG,
                                   "tier": "draft", "denoise": 0.78, "project": PROJ})
print("   首张:", code, first)
print("   二次:", post("/api/render", {"chain": "perspective", "image_path": IMG}))
wait(first["job"])

print("2) 代表链抽测（新走 bridge --config 的链各挑一条）")
run("效果图 draft", {"chain": "perspective", "image_path": IMG, "tier": "draft",
                     "denoise": 0.78, "project": PROJ})
run("彩平 draft", {"chain": "color_plan", "image_path": PROJ + r"\su\shots\04_总平面.png",
                   "tier": "draft", "denoise": 1.0, "project": PROJ})
run("轴测 draft", {"chain": "axon", "image_path": IMG, "tier": "draft",
                   "project": PROJ})
run("洗图 draft", {"chain": "enhance", "image_path": IMG, "tier": "draft",
                   "project": PROJ})
run("展板主图 draft", {"chain": "board_hero", "tier": "draft",
                       "prompt": "湿地景观，木质栈道，晨雾，竖构图",
                       "seed": 7, "project": PROJ})
run("局部改图 draft", {"chain": "inpaint", "image_path": IMG, "tier": "draft",
                       "box": [30, 20, 70, 55], "project": PROJ})
run("多角度 draft", {"chain": "multiangle", "image_path": IMG, "tier": "draft",
                     "angle": "aerial", "project": PROJ})
run("水彩 draft", {"chain": "watercolor", "image_path": IMG, "tier": "draft",
                   "project": PROJ})
run("扩图 draft", {"chain": "outpaint", "image_path": IMG, "tier": "draft",
                   "project": PROJ})
run("文生图室内 draft", {"chain": "interior_t2i", "tier": "draft",
                         "prompt": "阳光客厅绿植", "seed": 3, "project": PROJ})
run("文生图风景 draft", {"chain": "nature_t2i", "tier": "draft",
                         "prompt": "溪流森林", "seed": 4, "project": PROJ})
run("LoRA + realvis", {"chain": "lora", "image_path": IMG, "tier": "draft",
                       "denoise": 0.78, "model": "realvis", "project": PROJ})
run("叁十三平面 draft", {"chain": "sansan_plan", "image_path": PROJ + r"\su\shots\04_总平面.png",
                         "tier": "draft", "project": PROJ})
run("概念草图 一句话", {"chain": "concept_sketch", "tier": "draft",
                         "prompt": "滨水木平台公园，有廊架和树阵", "seed": 5, "project": PROJ})
run("平面图→3D白模", {"chain": "plan_to_mass", "image_path": PROJ + r"\su\shots\04_总平面.png",
                         "tier": "draft", "project": PROJ})

print("3) ipadapter（带参考图，参考图用底图自身）")
run("风格参考 draft", {"chain": "ipadapter", "image_path": IMG, "tier": "draft",
                       "style_path": IMG, "project": PROJ})

print("4) 越权路径（应 404）")
try:
    code, _, _ = get("/api/file?path=" + urllib.parse.quote(r"C:\Windows\win.ini"))
    print("   ", code)
except urllib.error.HTTPError as e:
    print("   ", e.code)
print("完成")
