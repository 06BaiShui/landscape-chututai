# -*- coding: utf-8 -*-
"""以 color_plan 模板为底克隆 sansan_plan：插入 LoRA 节点，其余参数照搬已验证配置。"""
import json

SRC = r"D:\ComfyUI\landscape_toolbox\workflows\api\color_plan_api.json"
DST = r"D:\ComfyUI\landscape_toolbox\workflows\api\sansan_plan_api.json"

w = json.load(open(SRC, encoding="utf-8"))

# node 13 = LoRA：checkpoint(1) -> lora -> KSampler.model；clip -> 正/负提示词
w["13"] = {
    "class_type": "LoraLoader",
    "inputs": {
        "model": ["1", 0],
        "clip": ["1", 1],
        "lora_name": "sansan_plan_XL_v1.safetensors",
        "strength_model": 0.5,
        "strength_clip": 0.5,
    },
}
w["2"]["inputs"]["clip"] = ["13", 0]
w["3"]["inputs"]["clip"] = ["13", 0]
w["10"]["inputs"]["model"] = ["13", 0]
# 触发词 + 平面描述（去掉 watercolor，避免和写实彩平打架）
w["2"]["inputs"]["text"] = (
    "SSS_landscape_plan, landscape architecture master plan, top view, "
    "large green lawn area, trees, light grey paving, natural stone, garden path, high quality"
)
json.dump(w, open(DST, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("克隆完成: sansan_plan_api.json = color_plan + LoRA(0.5) + 触发词")
