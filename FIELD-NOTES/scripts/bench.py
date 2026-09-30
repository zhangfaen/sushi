#!/usr/bin/env python3
"""通用 OpenAI 兼容 prefill/decode 测速脚本.

用法: bench.py <base_url> <model_id> <out_json>

测量方法 (Why): prefill 速度 = prompt_tokens / (首 token 延迟),
decode 速度 = 生成的 token 数 / (总耗时 - 首 token 延迟).
两台引擎 (sushi / mlx.serve) 都是 OpenAI 兼容接口, 用同一组 prompt、
同样的生成参数 (stream + max_tokens 固定), 保证可比性.

场景例子: 一个 2000 字的 prompt 交给模型续写 —— 你等第一个字的时间
由 prefill 速度决定, 之后每个字出来的快慢由 decode 速度决定.
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1].rstrip("/")
MODEL = sys.argv[2]
OUT = sys.argv[3]

# 三条覆盖不同场景的 prompt: 短指令 / 中文写作 / 长上下文(约2千字, 测大 prefill)
LONG_CTX = ("以下是一段关于分布式系统一致性的技术文档，请仔细阅读后总结要点。\n\n"
            + "在分布式系统中，一致性模型定义了读写操作在时间与空间上的可见性顺序。"
            * 80)  # 重复堆长上下文, ~7k chars

PROMPTS = [
    ("short", "Write a quick sort in Python with comments.", 200),
    ("chinese", "写一篇 300 字左右的短文，主题：早晨的咖啡馆。要求文笔细腻。", 400),
    ("longctx", LONG_CTX, 200),
]


def one_request(prompt, max_tokens):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": True,
        # mlx.serve / sushi 都支持 OpenAI 标准的 include_usage,
        # 最终 chunk 会带 usage; 不支持的引擎会忽略该字段
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    first = None
    usage = None
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                break
            chunk = json.loads(payload)
            # usage chunk 通常 choices 为空、只带 usage 字段
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices", []):
                if first is None and (ch.get("delta", {}).get("content")
                                      or ch.get("text")):
                    first = time.time()
    total = time.time() - t0
    if first is None:
        first = total
    if usage is None:
        # 流式没给 usage 时退化为估算
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
    pt = usage.get("prompt_tokens", 0)
    ct = usage.get("completion_tokens", 0)
    ttfb = first - t0
    gen_time = total - ttfb
    return {
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "ttfb_s": round(ttfb, 3),
        "total_s": round(total, 3),
        "prefill_tok_s": round(pt / ttfb, 1) if pt and ttfb > 0 else None,
        "decode_tok_s": round(ct / gen_time, 1) if ct and gen_time > 0 else None,
    }


def main():
    # 预热请求: 触发模型加载/编译, 不计时
    print("warmup...", flush=True)
    try:
        one_request("hi", 5)
    except Exception as e:
        print("warmup err:", e, flush=True)
    results = {}
    for name, prompt, mt in PROMPTS:
        print(f"bench {name}...", flush=True)
        # 每条跑 2 轮取最好值, 降低波动
        best = None
        for _ in range(2):
            try:
                r = one_request(prompt, mt)
                if best is None or (r["decode_tok_s"] or 0) > (best["decode_tok_s"] or 0):
                    best = r
            except Exception as e:
                print("  err:", e, flush=True)
                time.sleep(2)
        results[name] = best
        print(f"  {name}: prefill {best['prefill_tok_s']} tok/s, "
              f"decode {best['decode_tok_s']} tok/s "
              f"(ttfb {best['ttfb_s']}s, {best['prompt_tokens']} pt, "
              f"{best['completion_tokens']} ct)", flush=True)
    with open(OUT, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print("saved ->", OUT, flush=True)


if __name__ == "__main__":
    main()
