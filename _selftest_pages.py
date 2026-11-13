# -*- coding: utf-8 -*-
"""三个工作台页面 + 编辑/展板后端接口的端到端自测。

只测本地 HTTP 层：不启动/停止 ComfyUI，不出图，不碰用户的成果文件。
自测产生的临时文件会在最后精确删除（不删目录、不用通配符）。
用法：python _selftest_pages.py
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8760"
PNG1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
B64 = "data:image/png;base64," + PNG1
MADE = []
OK, BAD = [], []


def check(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(("  [OK] " if cond else "  [X]  ") + name + (("  " + str(extra)) if extra else ""))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=8) as r:
        return r.status, r.read()


def getj(path):
    s, b = get(path)
    return s, json.loads(b.decode("utf-8"))


def post(path, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    print("=" * 60)
    print("  出图台 / 编辑台 / 展板台 端到端自测")
    print("=" * 60)

    # 1. 三个页面
    print("\n[1] 页面与导航")
    for path, marker in (("/", "topnav"), ("/editor.html", "maskTools"), ("/board.html", "renderPNG")):
        try:
            s, b = get(path)
            t = b.decode("utf-8", "replace")
            check("GET %-16s 200 + 关键标记" % path, s == 200 and marker in t,
                  "%d bytes" % len(b))
        except Exception as e:
            check("GET " + path, False, e)
    _, t = get("/")
    html = t.decode("utf-8", "replace")
    check("主页面三个导航入口齐全",
          all(x in html for x in ('href="/editor.html"', 'href="/board.html"', 'id="toEditor"', 'id="toBoard"')))
    check("侧边栏补了编辑台/展板台说明",
          "区域重绘 · 编辑台" in html and "多图排版 · 展板台" in html)

    # 2. 状态与字体
    print("\n[2] 状态与字体探测")
    try:
        s, d = getj("/api/state")
        check("/api/state 可用", s == 200 and "categories" in d, "busy=%s" % d.get("busy"))
    except Exception as e:
        check("/api/state", False, e)
    try:
        s, d = getj("/api/fonts")
        names = [f["name"] for f in d.get("fonts", []) if f.get("installed")]
        check("/api/fonts 实测到已装字体", s == 200 and len(names) >= 3,
              "%d 个：%s" % (len(names), "、".join(names[:6])))
        check("/api/fonts 带 PS PostScript 字体名",
              any(f.get("ps") for f in d.get("fonts", [])),
              next((f["name"] + "→" + f["ps"] for f in d.get("fonts", []) if f.get("ps")), ""))
        for must in ("微软雅黑", "黑体", "宋体"):
            check("  含常用中文字体 " + must, must in names)
    except Exception as e:
        check("/api/fonts", False, e)

    # 3. 上传 / 保存 / 展板存取闭环
    print("\n[3] 上传 / 保存 / 展板布局闭环")
    up = None
    try:
        s, d = post("/api/upload_file", {"b64": B64, "name": "自测_1px.png", "kind": "source"})
        up = d.get("path")
        check("POST /api/upload_file 落盘", s == 200 and bool(up) and os.path.isfile(up), up)
        if up:
            MADE.append(up)
    except Exception as e:
        check("POST /api/upload_file", False, e)

    if up:
        try:
            s, b = get("/api/file?path=" + urllib.parse.quote(up))
            check("/api/file 能读回上传的图", s == 200 and b[:4] == b"\x89PNG")
        except Exception as e:
            check("/api/file", False, e)

    layout = {"version": 1, "name": "_自测展板", "w": 1346, "h": 951,
              "bg": "#ffffff", "grid": False, "nextId": 3,
              "els": [{"id": 1, "type": "text", "x": 40, "y": 40, "w": 600, "h": 80,
                       "text": "自测标题", "size": 48, "font": '"Microsoft YaHei",sans-serif',
                       "color": "#23281f", "bold": True, "align": "left", "rot": 0,
                       "opacity": 1, "lock": False, "hide": False}]}
    saved = None
    try:
        s, d = post("/api/save_file", {"b64": B64, "kind": "board", "name": "_自测展板",
                                       "project": None, "layout": layout})
        saved = d.get("path")
        check("POST /api/save_file 存 PNG + .layout.json",
              s == 200 and d.get("ok") and bool(saved) and os.path.isfile(saved)
              and bool(d.get("layout")) and os.path.isfile(d["layout"]),
              "%s / %s" % (saved, d.get("layout")))
        if saved:
            MADE.append(saved)
        if d.get("layout"):
            MADE.append(d["layout"])
        check("产物落在 出图/展板 或 _展板（不污染 AI 方案稿）",
              bool(saved) and ("展板" in saved) and ("AI方案稿" not in saved))
    except Exception as e:
        check("POST /api/save_file", False, e)

    if saved and os.path.isfile(saved[:-4] + ".layout.json"):
        lp = saved[:-4] + ".layout.json"
        try:
            s, d = getj("/api/board/load?path=" + urllib.parse.quote(lp))
            check("/api/board/load 读回布局一致",
                  s == 200 and d.get("layout", {}).get("name") == "_自测展板"
                  and len(d["layout"].get("els", [])) == 1)
        except Exception as e:
            check("/api/board/load", False, e)
    try:
        s, d = getj("/api/board/list")
        check("/api/board/list 能看到刚存的展板",
              s == 200 and any("_自测展板" in b.get("name", "") for b in d.get("boards", [])),
              "共 %d 份存档" % len(d.get("boards", [])))
    except Exception as e:
        check("/api/board/list", False, e)

    # 3b. Photoshop ExtendScript 导出
    print("\n[3b] Photoshop 脚本导出（UTF-8 BOM / CRLF / 中文不乱码）")
    jsx = ('#target photoshop\n'
           'var doc = app.documents.add(1346, 951, 300, "自测展板", '
           'NewDocumentMode.RGB, DocumentFill.WHITE);\n'
           '// 中文注释：图片按坐标精确置入\n')
    try:
        s, d = post("/api/save_text", {"text": jsx, "name": "_自测展板",
                                       "ext": ".jsx", "project": None})
        p = d.get("path")
        check("POST /api/save_text 落 .jsx", s == 200 and d.get("ok")
              and bool(p) and os.path.isfile(p), p)
        if p:
            MADE.append(p)
            with open(p, "rb") as f:
                raw = f.read()
            check("JSX 带 UTF-8 BOM（PS 读中文不乱码）", raw[:3] == b"\xef\xbb\xbf")
            check("JSX 用 CRLF 换行", b"\r\n" in raw and b"\n" in raw)
            check("JSX 中文内容完整", "中文注释" in raw.decode("utf-8-sig"))
            check("脚本落在 展板 目录（不污染 AI 方案稿）",
                  "展板" in p and "AI方案稿" not in p)
    except Exception as e:
        check("POST /api/save_text", False, e)

    # 4. 安全边界
    print("\n[4] 安全边界")
    try:
        s, _ = get("/api/open_dir?path=" + urllib.parse.quote("C:\\Windows"))
        check("目录白名单拒绝 C:\\Windows", s == 404)
    except urllib.error.HTTPError as e:
        check("目录白名单拒绝 C:\\Windows", e.code == 404)
    except Exception as e:
        check("目录白名单拒绝 C:\\Windows", False, e)
    try:
        s, d = post("/api/render", {"chain": "inpaint", "image_b64": B64,
                                    "filename": "自测.png", "prompt": "test"})
        check("inpaint 没圈区域时拦住（新校验生效）",
              s == 400 and "圈一块区域" in str(d.get("error", "")), d.get("error"))
    except Exception as e:
        check("inpaint 没圈区域时拦住", False, e)

    # 5. 遮罩参数链路：给 mask_b64 后应能建任务（ComfyUI 离线 → 退出码 9）
    print("\n[5] 任意形状遮罩 → 引擎 --mask 参数链路（ComfyUI 离线时只验到退出码 9）")
    if up:
        try:
            s, d = post("/api/render", {"chain": "inpaint", "image_path": up,
                                        "mask_b64": B64, "filename": "自测.png",
                                        "prompt": "open lawn with low ground cover",
                                        "denoise": 0.8, "tier": "review"})
            jid = d.get("job")
            check("mask_b64 被接受并建任务", s == 200 and bool(jid), d)
            if jid:
                final = None
                for _ in range(30):
                    _, j = getj("/api/job?id=" + jid)
                    if j.get("status") in ("done", "error"):
                        final = j
                        break
                    time.sleep(1)
                check("任务正常收尾（ComfyUI 未启动 → 9）",
                      bool(final) and final.get("code") == 9
                      and "ComfyUI" in str(final.get("error", "")),
                      final and {"code": final.get("code"), "error": final.get("error")})
        except Exception as e:
            check("mask 参数链路", False, e)

    # 6. 清理
    print("\n[6] 清理自测产物")
    for p in MADE:
        try:
            if os.path.isfile(p):
                os.remove(p)
                print("  已删 " + p)
        except Exception as e:
            print("  [!] 删除失败 %s : %s" % (p, e))
    d = os.path.dirname(MADE[-1]) if MADE else ""
    if d and os.path.isdir(d) and not os.listdir(d):
        try:
            os.rmdir(d)
            print("  已删空目录 " + d)
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("  通过 %d 项 / 失败 %d 项" % (len(OK), len(BAD)))
    if BAD:
        print("  失败：" + "、".join(BAD))
    print("=" * 60)
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
