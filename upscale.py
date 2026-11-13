# -*- coding: utf-8 -*-
"""4x-UltraSharp AI 超分：把一张 1152 长边的出图放到 2304（净 2x）。

为什么不走 comfy_bridge 的 inject()：那条路要求链里有 KSampler / 正负提示词，
而超分链没有采样。但协议（会话 / 上传 / 提交 / 轮询 / 下载）全部 import 复用，
不另写一份——见文件头「协议实现只留一份」的约定。

输出格式刻意和 run.py 保持一致（输出：/ 代价：），出图台直接复用它的解析。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.expanduser("~"), ".workbuddy", "skills",
                                "comfy-render", "scripts"))
import comfy_bridge as cb  # noqa: E402

UPSCALER = "4x-UltraSharp.pth"
API = "upscale_api.json"


def main():
    ap = argparse.ArgumentParser(description="AI 超分（4x 模型 + 缩回 2x）")
    ap.add_argument("-i", "--image", required=True, help="要放大的图")
    ap.add_argument("--project", help="项目目录名或完整路径")
    ap.add_argument("--out", help="直接指定输出目录")
    ap.add_argument("--scale", type=float, default=0.5, help="4x 之后的缩小系数，默认 0.5 = 净 2x")
    # 下面这些是出图台统一带的参数，超分链用不上，收下但忽略
    ap.add_argument("-p", "--prompt")
    ap.add_argument("--tier")
    ap.add_argument("--model")
    ap.add_argument("--denoise", type=float)
    ap.add_argument("-s", "--seed", type=int)
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        print("[X] 找不到输入图：%s" % args.image)
        return cb.EXIT["CONFIG"]

    s = cb.session()
    if not cb.online(s):
        print("[X] ComfyUI 没在跑：请先双击桌面「启动 ComfyUI」。")
        return cb.EXIT["OFFLINE"]

    wf_path = os.path.join(cb.TOOLBOX, "workflows", "api", API)
    if not os.path.isfile(wf_path):
        print("[X] 缺少超分模板：%s（先跑 _make_chains.py）" % wf_path)
        return cb.EXIT["CONFIG"]
    wf = cb.load_json(wf_path)

    name = cb.upload_image(s, args.image)
    wf["1"]["inputs"]["image"] = name
    wf["2"]["inputs"]["model_name"] = UPSCALER
    wf["4"]["inputs"]["scale_by"] = args.scale

    cfg = {"category": "通用", "out_subdir": "出图/AI方案稿", "chain": "upscale"}
    out_dir, _ = cb.resolve_out_dir(cfg, args, args.image)
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    print("超分：%s · 4x 模型 → 缩到 %.2f（净 %.1fx）"
          % (UPSCALER, args.scale, 4 * args.scale))
    try:
        pid = cb.submit(s, wf)
        print("已提交：%s" % pid[:8])
        rec, secs, peak = cb.poll(s, pid, 600, 1.5)
        files = cb.fetch(s, rec)
    except cb.BridgeError as e:
        print("[X] %s：%s" % (e.msg, e.detail or ""))
        return e.code

    stem = os.path.splitext(os.path.basename(args.image))[0]
    stem = stem.replace("_AI方案稿", "")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    written = []
    for i, (tag, data) in enumerate(files):
        suffix = "_%d" % i if len(files) > 1 else ""
        path = os.path.join(out_dir, "%s_2x超分_%s%s_AI方案稿.png" % (stem[:60], stamp, suffix))
        with open(path, "wb") as f:
            f.write(data)
        meta = {
            "kind": "ai_upscale", "upscaler": UPSCALER,
            "net_scale": 4 * args.scale, "scale_after_4x": args.scale,
            "source": os.path.abspath(args.image),
            "source_sha256": cb.sha256(args.image),
            "seconds": round(secs, 1), "vram_peak_gb": round(peak, 2),
            "prompt_id": pid, "cost_seconds": round(time.time() - t0, 1),
            "gate": "DRAFT_AI（方案表达稿，继承原图）",
            "note": "纯像素超分，不重绘、不改造型；不产生新的设计内容",
        }
        with open(os.path.splitext(path)[0] + ".meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        cb.register_draft(path, cfg, "AI 超分 2x（纯放大，不重绘）")
        print("输出：%s（%d KB）" % (path, len(data) // 1024))
        print("代价：%.1f 秒 · 峰值显存 %.1f GB · 落盘 %.1f MB"
              % (secs, peak, sum(len(x[1]) for x in files) / 1048576.0))
        written.append(path)
    print("FILES=" + ";".join(written))
    return cb.EXIT["OK"]


if __name__ == "__main__":
    raise SystemExit(main())
