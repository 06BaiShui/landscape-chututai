# -*- coding: utf-8 -*-
"""离线静态校验：链配置 / API 模板 / 节点映射 / 面板注册 四者一致。不需要 ComfyUI 在线。"""
import json, os, re, io, sys, urllib.request

API = r"D:/ComfyUI/landscape_toolbox/workflows/api"
CFG = r"D:/workplace/出图台/chains"
PANEL = r"D:/workplace/出图台/panel.py"
UI = r"D:/workplace/出图台/ui.html"

nm = json.load(open(os.path.join(API, "_node_map.json"), encoding="utf-8"))
ps = io.open(PANEL, encoding="utf-8").read()
us = io.open(UI, encoding="utf-8").read()
reg = dict(re.findall(r'"(\w+)":\s*\(os\.path\.join\(HERE, "chains", "([\w]+\.json)"\)', ps))

bad = []
for chain, cfgfile in reg.items():
    cp = os.path.join(CFG, cfgfile)
    if not os.path.isfile(cp):
        bad.append("%s: 缺配置 %s" % (chain, cfgfile)); continue
    c = json.load(open(cp, encoding="utf-8"))
    api = c["api"]
    if api not in nm:
        bad.append("%s: _node_map 未登记 %s" % (chain, api)); continue
    ap = os.path.join(API, api)
    if not os.path.isfile(ap):
        bad.append("%s: 缺模板 %s" % (chain, api)); continue
    wf = json.load(open(ap, encoding="utf-8"))
    # 节点引用完整性
    for nid, n in wf.items():
        for k, v in n["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                if v[0] not in wf:
                    bad.append("%s: 模板 node %s 引用了不存在的 %s" % (chain, nid, v[0]))
    # node_map 每个 (node, field) 必须真实存在
    # node_map 每个 (node, field) 必须真实存在（size / scale_to 是 fields 列表，单独处理）
    for key, loc in nm[api]["node_map"].items():
        ids = loc.get("nodes") or ([loc["node"]] if "node" in loc else [])
        flds = loc.get("fields") or ([loc["field"]] if "field" in loc else [])
        for nid in ids:
            for fld in flds:
                if nid not in wf or fld not in wf[nid]["inputs"]:
                    bad.append("%s: node_map[%s] 指向 %s.%s，模板里没有" % (chain, key, nid, fld))
    # 界面注册
    if chain + ":{" not in us and "'" + chain + "':" not in us:
        bad.append("%s: ui.html CHAINS 未注册" % chain)
    if "'" + chain + "'" not in us:
        bad.append("%s: ui.html 分组/SMALLS 未出现" % chain)

# 在线增强：按 ComfyUI 的节点签名校验每个模板的必填输入（能抓出手写模板漏连线）
try:
    info = json.load(urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=15))
    req = {k: list(v["input"]["required"]) for k, v in info.items()
           if isinstance(v.get("input"), dict)}
    checked = 0
    for chain, cfgfile in reg.items():
        cp = os.path.join(CFG, cfgfile)
        if not os.path.isfile(cp):
            continue
        c = json.load(open(cp, encoding="utf-8"))
        ap = os.path.join(API, c["api"])
        if not os.path.isfile(ap):
            continue
        wf2 = json.load(open(ap, encoding="utf-8"))
        checked += 1
        for nid, n in wf2.items():
            miss = [x for x in req.get(n["class_type"], []) if x not in n["inputs"]]
            if miss:
                bad.append("%s: 模板 node %s (%s) 缺必填 %s" % (chain, nid, n["class_type"], miss))
    print("在线节点验签:", checked, "个模板")
except Exception:
    print("在线节点验签: 跳过（ComfyUI 未启动，仅做离线一致性检查）")

# 分类一致性：library.py 的「三大类 / 具体链」必须与 ui.html GROUPS、panel.py 注册完全一致
try:
    import library
    m = re.search(r"const GROUPS = \[(.*?)\n\];", us, re.S)
    ui_groups = {}
    for title, ids in re.findall(r"\['([^']+)',\s*\[([^\]]+)\]\]", m.group(1) if m else ""):
        ui_groups[title] = re.findall(r"'(\w+)'", ids)
    for g in library.CATEGORIES:
        ui_list = ui_groups.get(g["name"])
        if ui_list is None:
            bad.append("分类 %s: ui.html GROUPS 缺这一组" % g["name"])
        elif ui_list != g["chains"]:
            bad.append("分类 %s: 链顺序/内容与界面不一致 (%d vs %d)"
                       % (g["name"], len(ui_list), len(g["chains"])))
    allc = {c for g in library.CATEGORIES for c in g["chains"]}
    if allc != set(reg):
        bad.append("分类表与面板注册链集合不一致：多 %s / 少 %s"
                   % (sorted(allc - set(reg)), sorted(set(reg) - allc)))
    for cid in allc:
        if cid not in library.CHAIN_LABELS:
            bad.append("分类表 %s 没有中文类别名" % cid)
    print("分类一致性:", len(allc), "条链 /", len(library.CATEGORIES), "大类")
except Exception as e:
    bad.append("分类校验失败: %s" % e)

print("面板注册链数:", len(reg))
print("_node_map 模板数:", len(nm))
print("问题数:", len(bad))
for b in bad: print("  !", b)
sys.exit(1 if bad else 0)
