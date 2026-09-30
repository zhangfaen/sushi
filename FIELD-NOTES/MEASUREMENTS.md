# 实测数据

所有数据来自同一台机器（Mac15,9 / 128 GB / macOS 26.7），两个引擎独占 GPU 轮流跑。
测量脚本见 [scripts/](scripts/)。

## 1. n-gram 表：4-bit 能否替代 bf16？

**能，差异可以忽略。** 4bpw 包默认带 95.4 GiB 的 bf16 n-gram 表，3bpw 包带同表的
4-bit 版本（29.8 GiB）。用 bf16 表版本做 teacher、4-bit 表版本做 student，
teacher-force 对比（16 提示词 × 512 token，kv8，7178 个有效位置）：

| 指标 | bf16 自比（sanity） | 4-bit 表 vs bf16 表 |
|---|---|---|
| KLD | 0.000000 | **0.001589** |
| top-1 一致率 | 100% | **98.83%** |
| cosine 相似度 | 1.000000 | 0.998689 |

参照系：该 4bpw 包相对**原始 bf16 模型**的 KLD 是 0.0632（旧权重）/ 0.0592（2026-09-30 新权重）。
也就是说，换 n-gram 表引入的偏差（0.0016）比量化本身的影响（0.06）小约 40 倍。

**结论**：用 4-bit 表，省 66 GiB 磁盘，质量损失在 1% 的 top-1 位置以内。

复现：

```bash
# teacher: bf16 表在位
python3 scripts/bench.py --help          # 见脚本说明
sushi kld capture --model <model-dir> --prompts prompts.jsonl \
  --out fixture --kv-quant 8 --tokens 512 --label bf16-table
# 换成 4-bit 表（物理换名，见 GOTCHAS #3），再 compare
sushi kld compare --model <model-dir> --fixture fixture \
  --kv-quant 8 --label 4bit-table --json 4bit_vs_bf16.json
```

## 2. 与 mlx.serve 的速度对比

对照对象：[`ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit`](https://huggingface.co/ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit)
（mlx.serve 26.9.6，权重 70.1 GiB 常驻）。测法：同一组 prompt、贪心解码、
流式请求带 `stream_options.include_usage`，每场景取 2 轮最好值。

| 场景 | sushi v1.0.4 | **sushi v1.0.5** | mlx.serve |
|---|---|---|---|
| decode 短指令（~21 token prompt） | 48.3 tok/s | **61.7** | 58.8 |
| decode 中文写作（~32 token prompt） | 38.1 | **41.2** | 40.1 |
| decode 长上下文（~1400 token prompt） | 39.7 | **44.3** | 46.1 |
| **prefill（全新长上下文，不命中缓存）** | 329 | **364** | **447** |

**怎么读**：

- 短 prompt 的 prefill 数值（100–260 tok/s）被网络往返噪声主导，没有参考意义；
  真正可比的是「全新长上下文」那一行。
- 长上下文第二次提问会命中前缀缓存，prefill 会飙到 3000–6400 tok/s——这反映的是
  缓存命中场景，不是真实 prefill 吞吐。测 prefill 必须用**全新 prompt**（换掉内容里的
  时间戳即可）。
- v1.0.5 的 decode 提升 8–28%（v1.0.5 release notes 提到更快的 MTP 验证与
  Flash-Next 解码优化），短 prompt 场景已**反超** mlx.serve；prefill 仍落后约 20%。

## 3. 质量与显存（官方 README 数据，供对照）

| 包 | 权重显存 | KLD | top-1 一致率 |
|---|---|---|---|
| oMLX oQ5e | 84.0 GiB | 0.0625 | 92.40% |
| **Sushi-4bpw（新权重）** | **63.7 GiB** | **0.0592** | — |
| Sushi-4bpw（旧权重） | 63.7 GiB | 0.0632 | 92.99% |
| mlx.serve mixed-4-8bit | 70.1 GiB | 0.0818 | 91.39% |
| Sushi-3bpw（新权重） | 49.3 GiB | 0.1036 | — |

即 sushi 用**更少**的显存达到**更低**的 KLD——这不是「多存一点换准一点」，
而是 EXL3 的 trellis-coded quantization 在同等比特率下失真更低（原理见
[`docs/engine-exl3-experts.md`](../docs/engine-exl3-experts.md)）。

## 4. 复现环境

```bash
python3 scripts/bench.py <base_url> <model_id> <out.json>
# 例:
python3 scripts/bench.py http://127.0.0.1:12345/v1 Qwen3.8-Flash-Next-Sushi-4bpw bench_sushi.json
python3 scripts/bench.py http://127.0.0.1:11234/v1 ddalcu/Qwen3.8-Flash-Next-MLX-Serve-mixed-4-8bit bench_mlx.json
```

注意：两个引擎不能同时跑（见 [GOTCHAS #6](GOTCHAS.md)），必须一个测完停掉再起另一个。