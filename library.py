# -*- coding: utf-8 -*-
"""成果库：出图的分类视图、直删、按「三大类 / 具体链」归档、撤销归档、缩略图。

设计边界（重要）
--------------
1. 只认 D:\\workplace\\设计项目 下、位于「出图\\AI方案稿」或「_未归档AI出图」里的图片，
   别的目录一律拒绝——这个端口不能变成任意文件删除器。
2. 删除 = 图片 + 同名 .meta.json / .gate.json / __方案表达稿使用说明.md 一起清，
   真正释放硬盘空间（不进回收站）。前端必须二次确认，后端再校验一遍白名单。
3. 归档 = 移动文件，绝不复制（复制会白占一份空间）。闸门 .gate.json 里存的是
   **绝对路径**，移动时必须同步改写 source / checklist，meta 的 outputs 同理，
   否则闸门状态和交付清单会断链。每次移动都写「分类索引.json」，可一键撤销。
4. 不碰闸门状态词（DRAFT_AI 等），不改 sha256，不改文件内容——只改路径字段。
"""
import glob
import io
import json
import os
import re
import shutil
import time

# 项目根：优先环境变量 CHUTAI_PROJECTS；否则用本机 D:\workplace\设计项目；
# 都没有（朋友的新机器）就退到仓库内的 项目/ 目录。
_DEF_ROOT = r"D:\workplace\设计项目"
ROOT = (os.environ.get("CHUTAI_PROJECTS")
        if os.environ.get("CHUTAI_PROJECTS")
        else (_DEF_ROOT if os.path.isdir(_DEF_ROOT)
              else os.path.join(os.path.dirname(os.path.abspath(__file__)), "项目")))
UNFILED = os.path.join(ROOT, "_未归档AI出图")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
INDEX_NAME = "分类索引.json"
LIST_NAME = "_出处清单.md"

# 三大类：与 ui.html 的 GROUPS 一一对应（_check_chains.py 会校验）
CATEGORIES = [
    {"dir": "01_方案表达", "name": "方案表达", "chains": [
        "concept_sketch", "concept_base", "plan_to_mass", "perspective", "aerial",
        "section_perspective", "axon", "facade", "interior", "massing", "sketch",
        "planting", "renov", "relight", "multiangle", "watercolor"]},
    {"dir": "02_图纸类", "name": "图纸类", "chains": [
        "color_plan", "sansan_plan", "plan_arch", "analysis_diagram",
        "sitediag", "style", "exploded"]},
    {"dir": "03_风格与修图", "name": "风格与修图", "chains": [
        "lora", "ipadapter", "material", "inpaint", "outpaint", "enhance",
        "interior_t2i", "nature_t2i", "board_hero"]},
]

# 具体出图类别（目录名 / 页面标题），与 ui.html CHAINS 的 name 保持一致
CHAIN_LABELS = {
    "concept_sketch": "概念草图·一句话", "concept_base": "概念底稿·线稿+提示词",
    "plan_to_mass": "平面图 → 轴测白模", "perspective": "白模 → 效果图",
    "aerial": "鸟瞰（实验）", "section_perspective": "剖透视",
    "axon": "轴测表现", "facade": "建筑立面", "interior": "室内空间",
    "massing": "体块推敲", "sketch": "草图渲染", "planting": "种植意向",
    "renov": "改造更新", "relight": "换氛围", "multiangle": "多角度（实验）",
    "watercolor": "水彩风格化",
    "color_plan": "总平 → 彩平图", "sansan_plan": "平面方案上色·叁十三",
    "plan_arch": "建筑总平彩图", "analysis_diagram": "分析图·图解",
    "sitediag": "现状分析图", "style": "插画风分析图", "exploded": "爆炸图",
    "lora": "中式院落 LoRA", "ipadapter": "风格参考", "material": "材质替换",
    "inpaint": "局部改图", "outpaint": "一键扩图", "enhance": "洗图",
    "interior_t2i": "文生图·室内", "nature_t2i": "文生图·风景",
    "board_hero": "展板主图",
    # 非链产物
    "upscale": "AI 放大 2x",
}
ALIASES = {"perspective_lora": "lora", "lora_siheyuan": "lora"}
GROUP_OF = {}
for _g in CATEGORIES:
    for _c in _g["chains"]:
        GROUP_OF[_c] = _g
GROUP_OF["upscale"] = CATEGORIES[2]
ALL_IDS = sorted(list(CHAIN_LABELS) + list(ALIASES), key=len, reverse=True)


def _abspath(p):
    return os.path.normpath(os.path.abspath(p))


def _roots(project=None, all_projects=False):
    """返回要扫描的 AI 出图根目录列表。"""
    roots = []
    if all_projects:
        for cat in sorted(os.listdir(ROOT)) if os.path.isdir(ROOT) else []:
            cdir = os.path.join(ROOT, cat)
            if not os.path.isdir(cdir) or cat.startswith(("_", ".")):
                continue
            for proj in sorted(os.listdir(cdir)):
                d = os.path.join(cdir, proj, "出图", "AI方案稿")
                if os.path.isdir(d):
                    roots.append(d)
    elif project:
        d = os.path.join(_abspath(project), "出图", "AI方案稿")
        if os.path.isdir(d):
            roots.append(d)
    # 没选项目 / 全量时都带上未归档目录
    if not project or all_projects:
        if os.path.isdir(UNFILED):
            for d in sorted(glob.glob(os.path.join(UNFILED, "*"))):
                if os.path.isdir(d):
                    roots.append(d)
    return roots


def _is_output_file(p):
    ap = _abspath(p)
    if not ap.lower().startswith(_abspath(ROOT).lower() + os.sep):
        return False
    if not ap.lower().endswith(IMG_EXT):
        return False
    parts = ap.lower().split(os.sep)
    return ("ai方案稿" in parts) or ("_未归档ai出图" in parts)


def _root_of(ap):
    """从文件路径反推它所属的 AI 出图根目录。"""
    cur = os.path.dirname(ap)
    while cur and len(cur) >= len(ROOT):
        base = os.path.basename(cur).lower()
        if base == "ai方案稿":
            return cur
        if base.isdigit() and os.path.basename(os.path.dirname(cur)).lower() == "_未归档ai出图":
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _sidecars(png):
    """图片 + 同名附属文件（meta / 闸门 / 使用说明）清单，只返回存在的。"""
    stem = os.path.splitext(png)[0]
    out = [png]
    for x in (stem + ".meta.json", png + ".meta.json", stem + ".gate.json",
              stem + "__方案表达稿使用说明.md"):
        if os.path.isfile(x) and x not in out:
            out.append(x)
    return out


def _load_meta(png):
    stem = os.path.splitext(png)[0]
    for p in (stem + ".meta.json", png + ".meta.json"):
        if os.path.isfile(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:
                return {}
    return {}


def _load_gate(png):
    p = os.path.splitext(png)[0] + ".gate.json"
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8")).get("status")
        except Exception:
            return None
    return None


def guess_chain(name, meta):
    c = meta.get("chain")
    if c in ALIASES:
        c = ALIASES[c]
    if c in CHAIN_LABELS:
        return c
    low = name.lower()
    for cid in ALL_IDS:
        if cid.lower() in low:
            return ALIASES.get(cid, cid)
    if "2x超分" in name:
        return "upscale"
    return None


def _tier(name, meta):
    if "__草稿_" in name:
        return "试方案·8步"
    if "__定稿_" in name:
        return "定稿·24步"
    steps = (meta.get("params") or {}).get("steps")
    if steps:
        return {8: "试方案·8步", 20: "汇报稿·20步", 24: "定稿·24步"}.get(steps, "%d 步" % steps)
    return "汇报稿·20步"


def _safe_dir(label):
    s = re.sub(r'[\\/:*?"<>|]', "-", label).strip().strip(".")
    return s or "未命名"


def _group_chain(chain):
    if chain and chain in GROUP_OF:
        g = GROUP_OF[chain]
        return g["dir"], CHAIN_LABELS.get(chain, chain), g["name"]
    return "00_未分类", "未标注出处", "未分类"


def describe(png, root):
    """把一张图还原成「具体出处」记录。"""
    meta = _load_meta(png)
    name = os.path.basename(png)
    chain = guess_chain(name, meta)
    gdir, label, gname = _group_chain(chain)
    src = meta.get("source_image") or ""
    ap = _abspath(png)
    rel = os.path.relpath(os.path.dirname(ap), root)
    cur_group = "" if rel == "." else rel.split(os.sep)[0]
    try:
        st = os.stat(ap)
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = 0, 0
    params = meta.get("params") or {}
    return {
        "path": ap, "name": name, "size": size, "mtime": mtime,
        "created": meta.get("created") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
        "chain": chain or "unknown", "chain_label": label,
        "group": gname, "group_dir": gdir, "cur_dir": rel,
        "archived": cur_group == gdir,
        "source": os.path.basename(src) if src else "（无 meta / 手工归档）",
        "source_full": src, "seed": meta.get("seed"),
        "steps": params.get("steps"), "tier": _tier(name, meta),
        "model": meta.get("model") or "—",
        "controlnet": ", ".join(meta.get("controlnet") or []) or "—",
        "source_size": meta.get("source_size") or "—",
        "seconds": meta.get("seconds"), "vram": meta.get("peak_vram_gb"),
        "prompt": (meta.get("prompt") or "")[:110],
        "gate": _load_gate(png) or "未登记",
        "project": meta.get("project_scope") or os.path.basename(os.path.dirname(os.path.dirname(root))),
        "has_meta": bool(meta),
    }


def scan(project=None, all_projects=False):
    roots = _roots(project, all_projects)
    items = []
    for root in roots:
        for png in glob.glob(os.path.join(root, "**", "*"), recursive=True):
            if not png.lower().endswith(IMG_EXT) or not os.path.isfile(png):
                continue
            items.append(describe(_abspath(png), root))
    items.sort(key=lambda x: -x["mtime"])
    groups = []
    for g in CATEGORIES + [{"dir": "00_未分类", "name": "未分类", "chains": []}]:
        buckets = {}
        for it in items:
            if it["group_dir"] == g["dir"]:
                buckets.setdefault(it["chain_label"], []).append(it)
        chains = [{"label": k, "items": v} for k, v in sorted(buckets.items())]
        groups.append({"dir": g["dir"], "name": g["name"],
                       "count": sum(len(c["items"]) for c in chains), "chains": chains})
    return {"items": items, "groups": groups,
            "total": len(items),
            "bytes": sum(i["size"] for i in items),
            "archived": sum(1 for i in items if i["archived"]),
            "roots": roots}


# ---------------------------------------------------------------- 删除

def delete(paths):
    ok, freed, rejected, errors = [], 0, [], []
    for p in paths:
        ap = _abspath(p)
        if not _is_output_file(ap) or not os.path.isfile(ap):
            rejected.append(ap)
            continue
        files = _sidecars(ap)
        for f in files:
            try:
                freed += os.path.getsize(f)
                os.remove(f)
                ok.append(f)
            except Exception as e:
                errors.append("%s: %s" % (f, e))
    # 顺手刷新受影响目录的出处清单
    for root in {_root_of(_abspath(p)) for p in paths if _is_output_file(_abspath(p))} - {None}:
        try:
            rebuild_lists(root)
        except Exception:
            pass
    return {"deleted": ok, "count": len([x for x in ok if x.lower().endswith(IMG_EXT)]),
            "freed": freed, "rejected": rejected, "errors": errors}


# ---------------------------------------------------------------- 归档 / 撤销

def _index_path(root):
    return os.path.join(root, INDEX_NAME)


def _read_index(root):
    p = _index_path(root)
    if not os.path.isfile(p):
        return []
    try:
        return json.load(open(p, encoding="utf-8")).get("moves", [])
    except Exception:
        return []


def _write_index(root, moves):
    p = _index_path(root)
    if moves:
        json.dump({"version": 1, "moves": moves}, open(p, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    elif os.path.isfile(p):
        os.remove(p)


def _rewrite_dir(text, old_dir, new_dir):
    """把附属文件里的「AI方案稿 旧目录」换成新目录。

    gate.json 的 checklist 指向 __方案表达稿使用说明.md，跟 png 不同名，
    所以必须按目录整体替换，不能只换图片文件名。md 里可能是单反斜杠，
    JSON 转抄的可能是双反斜杠，两种都换。
    """
    od, nd = old_dir.replace("\\", "\\\\"), new_dir.replace("\\", "\\\\")
    return text.replace(od, nd).replace(old_dir, new_dir)


def _move_one(src, dst, moves):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    pairs = []
    for f in _sidecars(src):
        rel = os.path.basename(f)
        d = os.path.join(os.path.dirname(dst), rel)
        if _abspath(f) == _abspath(d):
            continue
        if os.path.exists(d):
            raise RuntimeError("目标已存在：%s" % d)
        shutil.move(f, d)
        pairs.append([f, d])
    moves.append({"from": src, "to": dst, "files": pairs})
    # 同步改写附属文件里的绝对路径（按目录整体替换）
    old_dir, new_dir = os.path.dirname(src), os.path.dirname(dst)
    for old, new in pairs:
        if new.lower().endswith(".meta.json"):
            try:
                d = json.load(open(new, encoding="utf-8"))
                d["outputs"] = [_rewrite_dir(str(x), old_dir, new_dir) for x in (d.get("outputs") or [])]
                d["archived_from"] = src
                json.dump(d, open(new, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            except Exception:
                pass
        elif new.lower().endswith(".gate.json"):
            try:
                d = json.load(open(new, encoding="utf-8"))
                for k in ("source", "checklist", "stamped"):
                    if isinstance(d.get(k), str):
                        d[k] = _rewrite_dir(d[k], old_dir, new_dir)
                json.dump(d, open(new, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            except Exception:
                pass
        elif new.lower().endswith(".md"):
            try:
                t = io.open(new, encoding="utf-8").read()
                io.open(new, "w", encoding="utf-8").write(_rewrite_dir(t, old_dir, new_dir))
            except Exception:
                pass
    return pairs


def organize(project=None, all_projects=False, paths=None):
    data = scan(project, all_projects)
    todo = [i for i in data["items"] if not i["archived"]]
    if paths:
        want = {_abspath(p) for p in paths}
        todo = [i for i in todo if _abspath(i["path"]) in want]
    moved, skipped, errors = 0, 0, []
    by_root = {}
    for it in todo:
        root = _root_of(_abspath(it["path"]))
        if not root:
            skipped += 1
            continue
        dst = os.path.join(root, it["group_dir"], _safe_dir(it["chain_label"]),
                           os.path.basename(it["path"]))
        if _abspath(it["path"]) == _abspath(dst):
            skipped += 1
            continue
        if os.path.exists(dst):
            skipped += 1
            errors.append("目标已存在，跳过：%s" % os.path.basename(dst))
            continue
        try:
            _move_one(it["path"], dst, by_root.setdefault(root, _read_index(root)))
            moved += 1
        except Exception as e:
            errors.append("%s: %s" % (it["name"], e))
    for root, moves in by_root.items():
        _write_index(root, moves)
        rebuild_lists(root)
    return {"moved": moved, "skipped": skipped, "errors": errors,
            "roots": list(by_root)}


def undo_organize(project=None, all_projects=False):
    roots = _roots(project, all_projects)
    # 索引写在 AI方案稿 根 / 未归档日期目录下，也检查它们的父级
    cands = set(roots)
    for r in roots:
        cands.add(os.path.dirname(r))
    back, errors = 0, []
    for root in sorted(cands):
        idx = _index_path(root)
        if not os.path.isfile(idx):
            continue
        moves = _read_index(root)
        left = []
        for mv in reversed(moves):
            src, dst = mv["from"], mv["to"]
            if not os.path.isfile(dst):
                # 这张图后来被删了，索引里的记录直接作废
                continue
            # 反向搬：新路径 -> 原路径；顺带把附属文件路径字段改回去
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                pairs = []
                for old, new in reversed(mv.get("files", [])):
                    if not os.path.isfile(new):
                        continue
                    if os.path.exists(old):
                        raise RuntimeError("原位置已被占用：%s" % old)
                    shutil.move(new, old)
                    pairs.append([new, old])
                rev_old, rev_new = os.path.dirname(dst), os.path.dirname(src)
                for a, b in pairs:
                    if b.lower().endswith(".meta.json"):
                        try:
                            d = json.load(open(b, encoding="utf-8"))
                            d["outputs"] = [_rewrite_dir(str(x), rev_old, rev_new) for x in (d.get("outputs") or [])]
                            d.pop("archived_from", None)
                            json.dump(d, open(b, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                        except Exception:
                            pass
                    elif b.lower().endswith(".gate.json"):
                        try:
                            d = json.load(open(b, encoding="utf-8"))
                            for k in ("source", "checklist", "stamped"):
                                if isinstance(d.get(k), str):
                                    d[k] = _rewrite_dir(d[k], rev_old, rev_new)
                            json.dump(d, open(b, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                        except Exception:
                            pass
                    elif b.lower().endswith(".md"):
                        try:
                            t = io.open(b, encoding="utf-8").read()
                            io.open(b, "w", encoding="utf-8").write(_rewrite_dir(t, rev_old, rev_new))
                        except Exception:
                            pass
                back += 1
            except Exception as e:
                errors.append("%s: %s" % (os.path.basename(dst), e))
                left.append(mv)
        _write_index(root, left)
        rebuild_lists(root)
        # 清掉空的分类目录：先删已无图片的目录下的出处清单
        for g in [x["dir"] for x in CATEGORIES] + ["00_未分类"]:
            gd = os.path.join(root, g)
            if not os.path.isdir(gd):
                continue
            for sub in [gd] + glob.glob(os.path.join(gd, "*")):
                if not os.path.isdir(sub):
                    continue
                files = os.listdir(sub)
                if not any(f.lower().endswith(IMG_EXT) for f in files):
                    lst = os.path.join(sub, LIST_NAME)
                    if os.path.isfile(lst):
                        os.remove(lst)
            for sub in glob.glob(os.path.join(gd, "*")):
                if os.path.isdir(sub) and not os.listdir(sub):
                    os.rmdir(sub)
            if not os.listdir(gd):
                os.rmdir(gd)
    return {"restored": back, "errors": errors}


# ---------------------------------------------------------------- 出处清单

def _cell(v):
    return str(v if v not in (None, "") else "—").replace("|", "／").replace("\n", " ")


def rebuild_lists(root):
    """每个「具体出图类别」目录下写一份 _出处清单.md —— 文件夹层面的具体出处。"""
    lines_by_dir = {}
    for png in glob.glob(os.path.join(root, "**", "*"), recursive=True):
        if not png.lower().endswith(IMG_EXT) or not os.path.isfile(png):
            continue
        ap = _abspath(png)
        d = os.path.dirname(ap)
        r = describe(ap, root)
        lines_by_dir.setdefault(d, []).append(r)
    for d, recs in lines_by_dir.items():
        # 只给分类目录写清单，根目录不写
        if os.path.abspath(d) == os.path.abspath(root):
            continue
        head = "# 出处清单 · %s / %s\n\n" % (
            os.path.basename(os.path.dirname(d)), os.path.basename(d))
        head += "共 %d 张。归档目录按「三大类 / 具体出图类别」划分；下表记录每张图的具体出处。\n\n" % len(recs)
        head += ("| 文件 | 底图出处 | 项目 | 生成时间 | 档位 | 种子 | 底模 | 控制网 |\n"
                 "|---|---|---|---|---|---|---|---|\n")
        rows = []
        for r in sorted(recs, key=lambda x: x["created"], reverse=True):
            rows.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                _cell(r["name"]), _cell(r["source"]), _cell(os.path.basename(r["project"])),
                _cell(r["created"]), _cell(r["tier"]), _cell(r["seed"]),
                _cell(os.path.basename(r["model"])), _cell(r["controlnet"])))
        head += "\n".join(rows) + "\n\n"
        head += "## 逐张参数\n\n"
        for r in sorted(recs, key=lambda x: x["created"], reverse=True):
            head += "- **%s**\n  - 底图：`%s`（%s），输入尺寸 %s\n" % (
                r["name"], r["source"], r["source_full"] or "无记录", r["source_size"])
            head += "  - 提示词：%s\n" % _cell(r["prompt"] or "（无记录）")
            head += "  - 代价：%s s · 峰值显存 %s GB · 闸门状态 %s\n" % (
                _cell(r["seconds"]), _cell(r["vram"]), _cell(r["gate"]))
        try:
            io.open(os.path.join(d, LIST_NAME), "w", encoding="utf-8").write(head)
        except Exception:
            pass


# ---------------------------------------------------------------- 缩略图

_THUMBS = {}


def thumbnail(path, width=280):
    ap = _abspath(path)
    if not _is_output_file(ap):
        return None
    try:
        key = (ap, os.path.getmtime(ap), width)
    except OSError:
        return None
    if key in _THUMBS:
        return _THUMBS[key]
    from PIL import Image
    im = Image.open(ap).convert("RGB")
    im.thumbnail((width, width), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=78)
    data = buf.getvalue()
    if len(_THUMBS) > 400:
        _THUMBS.clear()
    _THUMBS[key] = data
    return data
