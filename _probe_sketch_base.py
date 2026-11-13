# -*- coding: utf-8 -*-
"""路线一「概念底稿」8GB 显存实测探针：只提交任务，不启停 ComfyUI 进程。

用法: python _probe_sketch_base.py A|B|C
  A = 单 Lineart 控制网 + 12 段式提示词（起步版）
  B = A + IPAdapter 水彩意向图
  C = A + IPAdapter 写实意向图（验证「换意向图换方向」）
"""
import json, os, sys, time, shutil, subprocess, urllib.request, urllib.error

BASE = 'http://127.0.0.1:8188'
IN = r'D:/AI/ComfyUI/Cache/input'
OUT = r'D:/workplace/_AI出图缓存/comfy_output'

CKPT = 'Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors'
UNION = 'controlnet-union-sdxl-1.0.safetensors'
SEED = 20261114
W, H = 1024, 640

# 画质 + 建筑形状 + 材料 + 视角 + 场景 + 风格 + 天气 + 灯光 + 色彩 + 氛围 + 渲染 + 镜头
POS = ("best quality, high detail, "
       "long horizontal pergola with slender columns and a flat roof beam, "
       "bamboo and wood, glass, "
       "human eye level view, "
       "snow-capped mountains, grass lawn and stone paved plaza, "
       "modern minimalist landscape architecture, "
       "sunny day, "
       "warm sunlight, soft shadows, "
       "natural green and warm beige tones, "
       "calm inviting atmosphere, "
       "architectural rendering, v-ray, realism, "
       "35mm lens")
NEG = ("text, watermark, logo, signature, blurry, low resolution, distorted geometry, "
       "duplicated buildings, warped lines, oversaturated, cartoon, plastic look, "
       "extra columns, floating objects, sketch, line drawing")

REFS = {
    'B': ('ref_watercolor.png', r'D:/workplace/出图台/上传/concept_sketch_00002__004809_sketch_2026_004938.png'),
    'C': ('ref_photoreal.png', r'D:/AI/ComfyUI/Cache/input/01_人视_perspective_20261113_210248_AI方案稿.png'),
}


def build(mode):
    w = {
        '1': {'class_type': 'CheckpointLoaderSimple', 'inputs': {'ckpt_name': CKPT}},
        '2': {'class_type': 'CLIPTextEncode', 'inputs': {'text': POS, 'clip': ['1', 1]}},
        '3': {'class_type': 'CLIPTextEncode', 'inputs': {'text': NEG, 'clip': ['1', 1]}},
        '4': {'class_type': 'LoadImage', 'inputs': {'image': '01_人视.png'}},
        '5': {'class_type': 'LineartStandardPreprocessor',
              'inputs': {'image': ['4', 0], 'guassian_sigma': 6.0,
                         'intensity_threshold': 8, 'resolution': 1024}},
        '6': {'class_type': 'ControlNetLoader', 'inputs': {'control_net_name': UNION}},
        '7': {'class_type': 'SetUnionControlNetType',
              'inputs': {'control_net': ['6', 0], 'type': 'canny/lineart/anime_lineart/mlsd'}},
        '8': {'class_type': 'ControlNetApplyAdvanced',
              'inputs': {'positive': ['2', 0], 'negative': ['3', 0], 'control_net': ['7', 0],
                         'image': ['5', 0], 'strength': 0.75,
                         'start_percent': 0.0, 'end_percent': 0.8}},
        '9': {'class_type': 'EmptyLatentImage', 'inputs': {'width': W, 'height': H, 'batch_size': 1}},
        '10': {'class_type': 'KSampler',
               'inputs': {'model': ['1', 0], 'seed': SEED, 'steps': 20, 'cfg': 6.0,
                          'sampler_name': 'dpmpp_2m', 'scheduler': 'karras',
                          'positive': ['8', 0], 'negative': ['8', 1],
                          'latent_image': ['9', 0], 'denoise': 1.0}},
        '11': {'class_type': 'VAEDecode', 'inputs': {'samples': ['10', 0], 'vae': ['1', 2]}},
        '12': {'class_type': 'SaveImage',
               'inputs': {'images': ['11', 0], 'filename_prefix': 'api/probe_' + mode}},
    }
    if mode in REFS:
        ref, _ = REFS[mode]
        w['13'] = {'class_type': 'LoadImage', 'inputs': {'image': ref}}
        w['14'] = {'class_type': 'IPAdapterUnifiedLoader',
                   'inputs': {'model': ['1', 0], 'preset': 'PLUS (high strength)'}}
        w['15'] = {'class_type': 'IPAdapterAdvanced',
                   'inputs': {'model': ['14', 0], 'ipadapter': ['14', 1], 'image': ['13', 0],
                              'weight': 0.65, 'weight_type': 'style transfer',
                              'combine_embeds': 'concat', 'start_at': 0.0, 'end_at': 1.0,
                              'embeds_scaling': 'V only'}}
        w['10']['inputs']['model'] = ['15', 0]
    return w


def vram():
    try:
        s = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
            timeout=5)
        return int(s.decode().strip().splitlines()[0])
    except Exception:
        return 0


def submit(wf):
    r = urllib.request.Request(BASE + '/prompt', data=json.dumps({'prompt': wf}).encode(),
                               headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=15) as x:
            return json.loads(x.read())['prompt_id'], None
    except urllib.error.HTTPError as e:
        return None, e.read().decode('utf-8', 'replace')[:900]


def wait(pid, timeout=420):
    t0 = time.time()
    peak = 0
    while time.time() - t0 < timeout:
        peak = max(peak, vram())
        try:
            d = json.loads(urllib.request.urlopen(BASE + '/history/' + pid, timeout=5).read())
            if pid in d:
                v = d[pid]
                st = v.get('status', {})
                if st.get('completed') or st.get('status_str') == 'success':
                    return 'done', peak, v, time.time() - t0
                if st.get('status_str') == 'error':
                    return 'error', peak, v, time.time() - t0
        except Exception:
            pass
        time.sleep(2)
    return 'timeout', peak, None, time.time() - t0


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else 'A').upper()
    if mode in REFS:
        ref, src = REFS[mode]
        dst = os.path.join(IN, ref)
        if not os.path.isfile(dst):
            shutil.copy(src, dst)
    wf = build(mode)
    pid, err = submit(wf)
    if err:
        print('提交被拒:', err)
        return 1
    print('已提交', mode, '| prompt_id', pid)
    status, peak, v, sec = wait(pid)
    print('状态:', status, '| 耗时 %.1f s' % sec, '| 峰值显存 %.2f GB' % (peak / 1024.0))
    if status == 'error':
        print('错误:', json.dumps(v.get('status', {}), ensure_ascii=False)[:900])
    if v:
        for out in v.get('outputs', {}).values():
            for im in out.get('images', []):
                p = os.path.join(OUT, im.get('subfolder', ''), im['filename'])
                mb = os.path.getsize(p) / 1048576.0 if os.path.isfile(p) else -1
                print('产物:', p, '| %.2f MB' % mb)
    return 0 if status == 'done' else 1


if __name__ == '__main__':
    sys.exit(main())
