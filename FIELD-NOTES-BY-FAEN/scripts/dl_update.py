#!/usr/bin/env python3
"""按清单强制重下指定文件（分段并行 + 断点续传 + 大小校验）.

用途 (Why): 作者更新模型时通常只重做部分文件（例如只重做 147 个专家层），
其余大件（ngram 表 102GB、trunk 等）没变。全量重下浪费几小时，
本脚本只按清单拉变更文件, 且对清单内文件强制覆盖——因为新旧文件
大小常常完全相同(同为 4bpw, 只是数值不同), 无法靠大小判断是否需要更新.

场景例子: 2026-09-30 作者发布 "new expert weights" (KLD 0.0632->0.0592),
只需重下 147 个专家层 + config.json 共 62GB, 而 102GB 的 ngram 表
和 6.3GB 的 trunk/embed/lm_head 原样保留.

用法: dl_update.py <repo> <dest_dir> <manifest.tsv> [workers]
manifest.tsv 每行: <size_bytes>\t<path>
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

CHUNK = 512 * 1024 * 1024
RETRY = 8
MIRROR = "https://hf-mirror.com"

REPO = sys.argv[1]
DEST = sys.argv[2]
MANIFEST = sys.argv[3]
WORKERS = int(sys.argv[4]) if len(sys.argv) > 4 else 12
PARTS_ROOT = os.path.join(DEST, "parts")

files_meta = []
chunk_jobs = []
with open(MANIFEST) as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        size_str, path = line.split("\t", 1)
        size = int(size_str)
        final = os.path.join(DEST, path)
        parts_dir = os.path.join(PARTS_ROOT, path)
        # 强制重下: 清掉可能残留的旧分段(同名 part 是旧文件内容, 混用会拼错)
        if os.path.isdir(parts_dir):
            shutil.rmtree(parts_dir)
        os.makedirs(parts_dir, exist_ok=True)
        n = (size + CHUNK - 1) // CHUNK
        url = f"{MIRROR}/{REPO}/resolve/main/{path}"
        files_meta.append((final, size, n, parts_dir))
        for i in range(n):
            start = i * CHUNK
            end = min(start + CHUNK, size) - 1
            chunk_jobs.append((path, i, url,
                               os.path.join(parts_dir, "part_%05d" % i), start, end))

total_bytes = sum(f[1] for f in files_meta)
print(f"更新 {len(files_meta)} 个文件 / {len(chunk_jobs)} 个分段, "
      f"共 {total_bytes / 1e9:.1f} GB, {WORKERS} 线程", flush=True)

work_q = queue.Queue()
for job in chunk_jobs:
    work_q.put(job)
failed = []
failed_lock = threading.Lock()


def curl_range(url, start, end, part_path):
    """下载 [start,end] 到 part_path, 支持续传; 精确大小才算成功."""
    expected = end - start + 1
    have = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    if have == expected:
        return True
    if have > expected:
        os.remove(part_path)
        have = 0
    cmd = ["curl", "-sL", "--max-time", "1800",
           "--speed-limit", "10240", "--speed-time", "60",
           "-r", "%d-%d" % (start + have, end), "-o", "-", url]
    with open(part_path, "ab") as out:
        subprocess.run(cmd, stdout=out, stderr=subprocess.DEVNULL)
    return os.path.exists(part_path) and os.path.getsize(part_path) == expected


def worker():
    while True:
        try:
            job = work_q.get_nowait()
        except queue.Empty:
            return
        _, _, url, part_path, start, end = job
        ok = False
        for attempt in range(RETRY):
            if curl_range(url, start, end, part_path):
                ok = True
                break
            time.sleep(min(2 * attempt + 1, 10))
        if not ok:
            with failed_lock:
                failed.append(part_path)
        work_q.task_done()


def done_bytes():
    done = 0
    for final, size, n, parts_dir in files_meta:
        for f in os.listdir(parts_dir):
            if f.startswith("part_"):
                done += os.path.getsize(os.path.join(parts_dir, f))
    return done


stop_report = threading.Event()


def reporter():
    last, last_t = done_bytes(), time.time()
    while not stop_report.wait(30):
        now, now_t = done_bytes(), time.time()
        rate = (now - last) / max(now_t - last_t, 1)
        eta = (total_bytes - now) / rate / 3600 if rate > 1000 else float("inf")
        print(f"[进度] {now / 1e9:.1f}/{total_bytes / 1e9:.1f} GB "
              f"({now / total_bytes * 100:.1f}%) {rate / 1e6:.1f} MB/s "
              f"ETA {eta:.2f}h 失败 {len(failed)}", flush=True)
        last, last_t = now, now_t


threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
for t in threads:
    t.start()
threading.Thread(target=reporter, daemon=True).start()
work_q.join()
stop_report.set()

if failed:
    print(f"{len(failed)} 个分段失败, 重跑本脚本续传:", flush=True)
    for f in failed[:10]:
        print("  FAIL " + f, flush=True)
    sys.exit(1)

print("[合并] 开始拼接...", flush=True)
for final, size, n, parts_dir in files_meta:
    with open(final, "wb") as out:
        for i in range(n):
            with open(os.path.join(parts_dir, "part_%05d" % i), "rb") as f:
                shutil.copyfileobj(f, out, 8 * 1024 * 1024)
    if os.path.getsize(final) != size:
        print(f"[错误] {final} 大小不符, 分段保留待查", flush=True)
        sys.exit(1)
    shutil.rmtree(parts_dir)
print("全部更新完成 ✓", flush=True)