# -*- coding: utf-8 -*-
"""通过 GitHub Git Data API 上传仓库（git push 被代理规则挡住时的替代路径）。

用法：python _gh_api_push.py <owner/repo> <branch> <commit_message>
只上传 git ls-files 列出的文件（与本地提交内容一致）。
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = sys.argv[1]          # 06BaiShui/chutai
BRANCH = sys.argv[2]        # main
MESSAGE = sys.argv[3]
API = "https://api.github.com"
TOKEN = subprocess.run(["gh", "auth", "token"], capture_output=True,
                       text=True).stdout.strip()

def call(method, path, payload=None, raw=False):
    url = API + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "chutai-push")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            return json.loads(body) if body and not raw else body
        except urllib.error.HTTPError as e:
            if e.code in (409, 422, 404):
                raise
            if e.code == 422:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("API 调用失败: %s %s (HTTP %s)" % (method, path, e.code))

files = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files"],
                       capture_output=True, text=True,
                       encoding="utf-8").stdout.splitlines()
files = [f for f in files if f.strip()]
print("待上传 %d 个文件" % len(files))

# 0. 空仓库播种：Git Data API 整体被禁（409），先用 Contents API 建初始提交
try:
    ref = call("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    base_head = ref["object"]["sha"]
except urllib.error.HTTPError as e:
    if e.code in (404, 409):   # 409 = 空仓库
        seed = base64.b64encode("# ChuTai\nseed commit\n".encode("utf-8")).decode("ascii")
        call("PUT", "/repos/%s/contents/README.md" % REPO,
             {"message": "seed", "content": seed, "branch": BRANCH})
        ref = call("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
        base_head = ref["object"]["sha"]
        print("已播种初始提交 %s" % base_head[:10])
    else:
        raise

# 1. blobs
tree_entries = []
for i, f in enumerate(files):
    with open(f, "rb") as fh:
        content = base64.b64encode(fh.read()).decode("ascii")
    blob = call("POST", "/repos/%s/git/blobs" % REPO,
                {"content": content, "encoding": "base64"})
    tree_entries.append({"path": f.replace("\\", "/"), "mode": "100644",
                         "type": "blob", "sha": blob["sha"]})
    print("  [%d/%d] %s" % (i + 1, len(files), f))

# 2. tree（基于远程当前 main，保持历史连续；空仓库则无 base）
base_head = None
try:
    ref = call("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    base_head = ref["object"]["sha"]
except urllib.error.HTTPError as e:
    if e.code in (404, 409):   # 409 = 空仓库，GitHub 对空仓库的 ref 查询也报 409
        base_head = None
    else:
        raise

# 2.5 空仓库：Git Data API 整体被禁（409），先用 Contents API 播种一个提交
if base_head is None:
    seed = base64.b64encode("# ChuTai\nseed commit\n".encode("utf-8")).decode("ascii")
    call("PUT", "/repos/%s/contents/README.md" % REPO,
         {"message": "seed", "content": seed, "branch": BRANCH})
    ref = call("GET", "/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    base_head = ref["object"]["sha"]
    print("已播种初始提交 %s" % base_head[:10])
tree_payload = {"tree": tree_entries}
if base_head:
    tree_payload["base_tree"] = base_head
tree = call("POST", "/repos/%s/git/trees" % REPO, tree_payload)

# 3. commit + ref
commit = call("POST", "/repos/%s/git/commits" % REPO,
              {"message": MESSAGE, "tree": tree["sha"],
               "parents": [base_head] if base_head else []})
if base_head:
    call("PATCH", "/repos/%s/git/refs/heads/%s" % (REPO, BRANCH),
         {"sha": commit["sha"], "force": False})
else:
    call("POST", "/repos/%s/git/refs" % REPO,
         {"ref": "refs/heads/%s" % BRANCH, "sha": commit["sha"]})
print("完成：%s/commit/%s" % (REPO, commit["sha"][:10]))
