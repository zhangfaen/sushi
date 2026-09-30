# FIELD-NOTES

第三方使用者在真实环境里的部署、实测与踩坑记录。**这不是官方文档。**

官方原理性文档见 [`docs/`](../docs/)：架构、kernel、pack 格式、KLD 测量方法、性能基线。
这里只记官方文档没覆盖的实操部分——尤其是**中国大陆网络环境下的下载方案**、同机跨引擎对比、
以及那些只有踩过才知道的报错和解法。

## 环境

| 项 | 值 |
|---|---|
| 机器 | Mac15,9 / 128 GB 统一内存 |
| 系统 | macOS 26.7（Darwin 25.6；sushi 要求 ≥ 26.2） |
| sushi | v1.0.5 |
| 模型 | `beamster/Qwen3.8-Flash-Next-Sushi-4bpw` + 4-bit n-gram 表 |
| 实测占用 | 权重 63.7 GiB 常驻 GPU，磁盘 190 GB（含 102 GB 的 bf16 n-gram 表） |

## 目录

| 文件 | 内容 |
|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | 从零到能推理：下载、启动、验证、接入 OpenAI 客户端 |
| [GOTCHAS.md](GOTCHAS.md) | 踩坑清单，**按报错信息检索** |
| [MEASUREMENTS.md](MEASUREMENTS.md) | 实测数据：n-gram 表质量对比、与 mlx.serve 的速度对比 |
| [UPGRADING.md](UPGRADING.md) | 怎么发现作者更新、怎么只下载变更文件（62 GB 而非 190 GB） |
| [scripts/](scripts/) | 可复用脚本：下载器、测速、KLD 对比 |
| [prompts.jsonl](prompts.jsonl) | KLD 对比用的 16 条提示词（中英混合、多领域） |

## 三条最有价值的结论

1. **中国网络下最快的下载路线是 hf-mirror 直连 + 分段并行**：实测聚合 46 MB/s，
   比走代理（6 MB/s）快 7 倍以上。但 `hf` CLI 与镜像不兼容会卡死，必须用自写下载器。
2. **4-bit n-gram 表可以放心替代 95 GiB 的 bf16 表**：实测 KLD 只差 0.0016、
   top-1 一致率 98.8%，比模型量化本身的影响小 40 倍，省 66 GiB 磁盘。
3. **sushi 的 EXL3 量化质量优于 mlx.serve 的 affine 量化**（KLD 0.0592 vs 0.0818），
   而 v1.0.5 之后 decode 速度也追平了；代价是 prefill 仍慢约 20%。