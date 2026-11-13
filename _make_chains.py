# -*- coding: utf-8 -*-
"""给 ComfyUI 工具箱新增「用了别的模型」的工作流。可重复运行，幂等。

新增两条：
  1) perspective_lora_api.json —— 在底模与采样器之间挂 LoraLoader（四合院建筑 LoRA）
     这条能走现有 comfy_bridge 引擎，因为 KSampler 还在。
  2) upscale_api.json —— 4x-UltraSharp 纯超分，无采样节点，由 upscale.py 单独驱动。

只改 workflows/api/ 下的文件 + _node_map.json，不动任何已有链。
"""
import json
import os
import shutil

API = r"D:\ComfyUI\landscape_toolbox\workflows\api"
NM = os.path.join(API, "_node_map.json")
CHAINS = r"D:\workplace\出图台\chains"
LORA = "sdxl_architecture_siheyuan.safetensors"
UPSCALER = "4x-UltraSharp.pth"


def read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write(p, obj):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def backup_once(p, tag):
    dst = p + ".bak_" + tag
    if not os.path.isfile(dst):
        shutil.copy2(p, dst)
        print("备份:", dst)
    return dst


# ---------------------------------------------------------------- 1. LoRA 链
src = read(os.path.join(API, "perspective_api.json"))
wf = dict(src)
wf["17"] = {
    "class_type": "LoraLoader",
    "inputs": {
        "model": ["1", 0], "clip": ["1", 1],
        "lora_name": LORA,
        "strength_model": 0.65, "strength_clip": 0.65,
    },
}
changed = []
for nid, node in wf.items():
    if nid == "17":
        continue
    for key, val in node.get("inputs", {}).items():
        if not isinstance(val, list) or len(val) != 2:
            continue
        if key == "model" and val == ["1", 0]:
            node["inputs"][key] = ["17", 0]
            changed.append((nid, node["class_type"], "model"))
        elif key == "clip" and val == ["1", 1]:
            node["inputs"][key] = ["17", 1]
            changed.append((nid, node["class_type"], "clip"))
write(os.path.join(API, "perspective_lora_api.json"), wf)
print("LoRA 模板: perspective_lora_api.json，改接", len(changed), "处")
for x in changed:
    print("   ", x)

# ---------------------------------------------------------------- 2. 超分链
up = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "input.png"}},
    "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": UPSCALER}},
    "3": {"class_type": "ImageUpscaleWithModel",
          "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
    "4": {"class_type": "ImageScaleBy",
          "inputs": {"image": ["3", 0], "upscale_method": "lanczos", "scale_by": 0.5}},
    "5": {"class_type": "SaveImage",
          "inputs": {"filename_prefix": "skill/upscale", "images": ["4", 0]}},
}
write(os.path.join(API, "upscale_api.json"), up)
print("超分模板: upscale_api.json（4x 放大后降 0.5 → 净 2x）")

# ---------------------------------------------------------------- 3. 节点映射
backup_once(NM, "20261113_lora")
nm = read(NM)
base = nm["perspective_api.json"]
entry = dict(base)
entry["file"] = "perspective_lora_api.json"
entry["label"] = "效果图 + 四合院建筑 LoRA"
entry["preset"] = "perspective_lora"
entry["nodes"] = 17
entry["output_size"] = "由输入图决定（长边 1152）"
entry["lora"] = {"node": "17", "name": LORA,
                 "strength_model": 0.65, "strength_clip": 0.65}
nm["perspective_lora_api.json"] = entry
write(NM, nm)
print("节点映射: 新增 perspective_lora_api.json 条目")

# ---------------------------------------------------------------- 4. 链配置
os.makedirs(CHAINS, exist_ok=True)
cfg = {
    "skill": "comfy-render",
    "chain": "perspective_lora",
    "api": "perspective_lora_api.json",
    "label": "效果图 + 四合院建筑 LoRA",
    "category": "通用",
    "technical": False,
    "needs_image": True,
    "long_side": 1152,
    "model": "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
    "preset_prompt": (
        "landscape architecture perspective, chinese courtyard, siheyuan, "
        "timber pavilion, grey tile roof, bluestone paving, courtyard trees, "
        "dusk, warm light, realistic render, architectural visualization, high detail"),
    "style_tail": "realistic render, architectural visualization, soft shadows, high detail",
    "negative_extra": "extra structures, floating objects, duplicated elements",
    "denoise_default": 0.78,
    "denoise_range": [0.55, 0.9],
    "timeout": 900,
    "retries": 2,
    "poll_interval": 1.5,
    "out_subdir": "出图/AI方案稿",
    "lora": {"name": LORA, "strength_model": 0.65, "strength_clip": 0.65},
}
write(os.path.join(CHAINS, "lora_siheyuan.json"), cfg)
print("链配置:", os.path.join(CHAINS, "lora_siheyuan.json"))

# ---------------------------------------------------------------- 5. 全链薄配置
# 出图台 24 条链统一走 comfy_bridge --config；这里从 _node_map.json 批量生成薄链配置。
# long_side / denoise / steps 不写：bridge 自己从 node_map 的 meta 里取。
PRESET = {
    "perspective": "landscape architecture perspective, realistic render, architectural visualization, soft shadows, high detail",
    "aerial": "aerial view of residential landscape, realistic render, architectural visualization, soft shadows, high detail",
    "section_perspective": "architectural section perspective, realistic materials, warm interior light, high detail",
    "axon": "axonometric architectural illustration, clean material expression, soft shadows, high detail",
    "facade": "architectural facade, timber screen and glass, realistic material texture, high detail",
    "interior": "modern interior design, warm lighting, wood floor, realistic render, high detail",
    "massing": "architectural massing study, white volume with material hints, soft shadows, high detail",
    "sketch": "architectural sketch render, marker style, quick design expression",
    "planting": "landscape planting design, flower border, ornamental grasses, seasonal colors, high detail",
    "renov": "building renovation design, preserved brick structure with modern insertion, high detail",
    "relight": "atmosphere change, night lighting with warm glow, high detail",
    "multiangle": "same project seen from another angle, consistent style, high detail",
    "color_plan": "master plan color rendering, landscape plan, clean color blocks, high quality",
    "plan_arch": "architectural site plan color rendering, roof texture, landscape surroundings, high quality",
    "analysis_diagram": "architecture analysis diagram, flat illustration style, clean vector look, soft colors, high quality",
    "sitediag": "site analysis diagram, flat illustration, highlight the project site, clean style",
    "style": "illustration style analysis drawing, watercolor texture, hand drawn look",
    "exploded": "exploded axonometric diagram, layered components, light background, high detail",
    "ipadapter": "same composition and content as input, style transferred from reference image, high detail",
    "material": "material replacement only, keep geometry unchanged, realistic texture, high detail",
    "inpaint": "seamless edit inside the mask only, realistic render, high detail",
    "enhance": "enhanced details and texture, photorealistic, high detail",
    "board_hero": "award winning landscape architecture, timber boardwalk through wetland, morning mist, cinematic light, high detail",
}
TECH = {"analysis_diagram", "sitediag", "style", "exploded", "board_hero"}
nm_all = read(NM)
made = 0
for fname, meta in sorted(nm_all.items()):
    if not fname.endswith("_api.json") or fname.startswith("upscale"):
        continue
    cid = fname.replace("_api.json", "")
    out = os.path.join(CHAINS, cid + ".json")
    if os.path.isfile(out) and cid != "perspective":     # 已有的不覆盖，perspective 每次刷新
        continue
    dn = meta.get("denoise")
    write(out, {
        "skill": "comfy-render",
        "chain": cid,
        "api": fname,
        "label": meta.get("label") or cid,
        "category": "技术图" if cid in TECH else "通用",
        "technical": cid in TECH,
        "needs_image": cid != "board_hero",
        "model": "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
        "preset_prompt": PRESET.get(cid) or "landscape architecture, high detail",
        "style_tail": "realistic render, architectural visualization, soft shadows, high detail",
        "negative_extra": "extra structures, floating objects, duplicated elements",
        "denoise_default": dn if dn is not None else 0.7,
        "timeout": 900,
        "retries": 2,
        "poll_interval": 1.5,
        "out_subdir": "出图/AI方案稿",
    })
    made += 1
print("薄链配置生成:", made, "条 ->", CHAINS)
print("完成")
