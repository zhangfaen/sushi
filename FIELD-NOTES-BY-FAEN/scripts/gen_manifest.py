#!/usr/bin/env python3
"""从 HuggingFace 仓库生成下载清单（TSV: <size_bytes>\\t<path>），供 dl_update.py 使用。

三种模式（Why: 全量下载要 190GB，而作者通常只重做部分文件，增量更新只要 62GB）：

  --exclude PATTERN   全量，但排除匹配的文件（可多次；常用 'assets/*' 跳过 README 配图）
  --include PATTERN   只列出匹配的文件（可多次；如只要 ngram_table.bin）
  --diff OLD_REV      只列出相对旧版本 OLD_REV 变更或新增的文件

  --diff 是增量更新的关键：HF 的 tree API 每个文件带 oid（blob 哈希），
  比对当前 main 与旧 revision 的 oid 即可精确算出差异。**不要用文件大小判断**——
  重新量化的文件（如 4bpw 专家层换新调优技术）大小常常完全相同。

用法:
  gen_manifest.py <repo> --exclude 'assets/*' --out manifest.tsv
  gen_manifest.py <repo> --include 'ngram_table.bin' --out ngram.tsv
  gen_manifest.py <repo> --diff 79ecdf1251 --out update.tsv

OLD_REV 从哪来: `curl -s https://hf-mirror.com/api/models/<repo>/commits/main`
                挑日期与你上次下载时间吻合的那个 commit sha。
"""
import argparse
import fnmatch
import json
import sys
import urllib.request

# 国内直连 hf-mirror 最快（实测 46MB/s）；如需官方源改成 https://huggingface.co
MIRROR = "https://hf-mirror.com"


def fetch_tree(repo, revision):
    """取仓库文件树: {path: (oid, size)}。"""
    url = f"{MIRROR}/api/models/{repo}/tree/{revision}?recursive=true"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    return {x["path"]: (x.get("oid"), x.get("size", 0))
            for x in data if x["type"] == "file"}


def matches(path, patterns):
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="如 beamster/Qwen3.8-Flash-Next-Sushi-4bpw")
    ap.add_argument("--exclude", action="append", default=[],
                    help="排除匹配的文件（可多次）")
    ap.add_argument("--include", action="append", default=[],
                    help="只保留匹配的文件（可多次）")
    ap.add_argument("--diff", metavar="OLD_REV",
                    help="只列出相对该 revision 变更/新增的文件")
    ap.add_argument("--out", required=True, help="输出 TSV 路径")
    args = ap.parse_args()

    current = fetch_tree(args.repo, "main")

    if args.diff:
        old = fetch_tree(args.repo, args.diff)
        selected = {p: v for p, v in current.items()
                    if p not in old or old[p][0] != v[0]}
        changed = len(selected)
        added = sum(1 for p in selected if p not in old)
        print(f"相对 {args.diff}: {changed} 个文件变更/新增"
              f"（其中新增 {added}）", file=sys.stderr)
    else:
        selected = dict(current)

    if args.include:
        selected = {p: v for p, v in selected.items() if matches(p, args.include)}
    if args.exclude:
        selected = {p: v for p, v in selected.items() if not matches(p, args.exclude)}

    if not selected:
        print("警告: 清单为空，检查 --include/--exclude 模式", file=sys.stderr)

    with open(args.out, "w") as f:
        for path in sorted(selected):
            f.write(f"{selected[path][1]}\t{path}\n")

    total = sum(v[1] for v in selected.values())
    print(f"写入 {len(selected)} 个文件, 共 {total / 1e9:.1f} GB -> {args.out}",
          file=sys.stderr)


if __name__ == "__main__":
    main()