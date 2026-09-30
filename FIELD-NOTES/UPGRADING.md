# 升级实录：怎么发现更新、怎么只下载变更部分

作者更新很频繁（模型权重几乎每次都是重做**部分**文件）。全量重下要 190 GB，
而按下面的方法只下变更文件，2026-09-30 那次升级只用了 62.1 GB。

## 1. 发现更新

**二进制**（GitHub release）：

```bash
curl -s https://api.github.com/repos/beamivalice/sushi/releases/latest \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['tag_name'], d['published_at'])"
# 对比本地：sushi --version
```

**模型权重**（HuggingFace commit 历史）：

```bash
curl -s "https://hf-mirror.com/api/models/<repo>/commits/main" \
  | python3 -c "
import json,sys
for c in json.load(sys.stdin)[:5]:
    print(c['id'][:10], c['date'][:19], '|', c['title'])"
```

commit 标题通常直接说明改了什么，例如
`Sushi-4bpw: new expert weights, KLD 0.0592`。

## 2. 精确算出哪些文件变了

HF 的 tree API 支持指定 revision，且每个文件带 `oid`（blob 哈希）。用当前版本
（`main`）与「你本地那份对应的旧 revision」对比 oid，就能精确列出差异：

```bash
python3 scripts/gen_manifest.py <repo> --diff <old-revision> --out update.tsv
```

`<old-revision>` 是上次下载时对应的 commit sha（从 commits 历史里挑日期匹配的那个）。

**关键坑**：新旧量化文件**大小可能完全相同**（同为 4bpw，只是数值不同），
所以**不能靠文件大小判断是否需要更新**，必须比对 oid。

## 3. 下载变更文件

```bash
python3 scripts/dl_update.py <repo> <model-dir> update.tsv 12
```

该脚本对清单内文件**强制重下**（先清掉可能残留的同名旧分段，避免拼错），
对清单外的大件（如 102 GB 的 n-gram 表）完全不动。

## 4. 2026-09-30 升级实录

作者的推文 + commit `6bc880fdbf`（`Sushi-4bpw: new expert weights, KLD 0.0592`）：

| 项 | 变化 |
|---|---|
| 量化技术 | 新的 EXL3 专家层调优：`quantizer: ldlq`、`codebook: mcg`、`window 15`、`out_scales: svh`（旧的 `out_scales_mode: auto` 被替换） |
| 质量 | KLD 0.0632 → **0.0592**（优于 oMLX oQ5e 的 0.0625） |
| 显存 | 不变，63.7 GiB |
| **变更文件** | **147 个专家层 + `config.json` = 62.1 GB** |
| 未变更 | `ngram_table.bin`（102 GB）、trunk、embed、lm_head、MTP、tokenizer |
| 二进制 | v1.0.4 → v1.0.5（新增 2.6bpw 支持、更快的 Flash-Next 解码与 MTP 验证、长对话缓存、修复强制 tool calls、`--preserve-thinking`） |
| 实测速度 | decode +8~28%，prefill +11%（详见 [MEASUREMENTS.md](MEASUREMENTS.md)） |

升级后 `sushi serve` 日志确认新量化参数被识别：`[expert-exl3] engaged K=4 codebook=mcg window=15`。

## 5. 注意事项

- **二进制与权重最好一起更新**。本次 v1.0.5 比新权重早发布两天，且 `config.json` 的
  量化参数字段有变（`out_scales_mode` → `out_scales`），旧二进制能否正确解释新权重
  无法从 release notes 确认。二进制只有 67 MB，一起更新最省事、也规避了不确定性。
- **更新前保留旧二进制备份**，出问题可整体回退：

  ```bash
  mv sushi-macos-arm64 sushi-macos-arm64-<旧版本>.bak   # 218 MB
  ```
- **更新权重后首次启动可能报内存不足**：刚下载几十 GB 文件会让 page cache 占满、
  `free` 掉到 1 GB 以下，触发预检误报。加 `--skip-mem-preflight`（见 [GOTCHAS #2](GOTCHAS.md)）。
- **作者会删掉已废弃参数的文档**。本次 `--mtp-typical` 的说明段落被专门 commit 删除，
  实际参数是否仍生效未验证——升级后按新文档行事，别沿用旧命令。