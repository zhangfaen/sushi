# 踩坑清单

按**报错信息**检索。每条给出症状、原因、解法。

## 1. `MISSING WEIGHT: ...switch_mlp.gate_proj.weight` → LoadFailed

**症状**：模型文件全部完整（逐文件比对 API 的字节数无误），启动却报某个专家权重缺失。

**原因**：`--model` 用了**相对路径**（如 `--model ./models/xxx`）。sushi 在相对路径下解析
权重分片有 bug，找不到文件就报缺权重。

**解法**：改用绝对路径。

```bash
# 错误
./sushi serve --model ./models/Qwen3.8-Flash-Next-Sushi-4bpw
# 正确
./sushi serve --model /abs/path/to/models/Qwen3.8-Flash-Next-Sushi-4bpw
```

**排查要点**：这个报错和「文件真的缺失」长得一样。先确认文件在不在（对照 HF API 的
文件清单逐个比字节数），如果都在，那就是路径问题。

## 2. `InsufficientMemory: needs ~70.7 GB ... but only 63.9 GB is available` → LoadFailed

**症状**：之前在同样配置下能加载，现在报内存不足；或者刚下载完几十 GB 模型后首次启动就报。

**原因**：内存预检只看 **free RAM**，而 macOS 会把刚读写的文件放进 page cache，
`free` 掉到 1 GB 以下、但几十 GB 处于 inactive/speculative（可回收）状态。
预检把可回收缓存当成不可用，于是误报。

**解法**：加 `--skip-mem-preflight`（README 明确支持这种场景，macOS 会在 MLX 分配时回收文件缓存）。

```bash
sushi serve --model /abs/path --skip-mem-preflight
```

**先验证确实够内存**，再跳过预检（真 over-commit 会让服务 hard crash）：

```bash
vm_stat | head -6   # 看 Pages free / inactive / speculative
# 实际可回收 ≈ free + inactive + speculative + purgeable
```

实测：需要 70.7 GB，`free` 只有 0.7 GB，但可回收 75.7 GB（`inactive` 34.3 + `speculative` 40.6），
加 `--skip-mem-preflight` 后正常加载。

## 3. 换了 n-gram 表却没生效（日志仍显示旧的 bits/大小）

**症状**：把 4-bit 表放进目录、改了 `config.json` 的 `ngram_table.file` 指向它，
启动日志却仍然加载 bf16 表。

**原因**：sushi **只认文件名 `ngram_table.bin`**。`config.json` 里 `ngram_table.file`
指向其他文件名不生效；把该文件藏起来则直接 `FileNotFound`。
而 `bits` / `group_size` 是**从文件内嵌的 meta 自动识别**的（文件头 8 字节是 header 长度，
随后是 JSON metadata），与 `config.json` 里写的值无关——实测 `config.json` 写 `bits: 16`
而文件实际是 4-bit 时，日志正确显示 `4 bits (29.8 GB)`。

**解法**：物理换名，并让 `config.json` 与文件一致（后者是官方 README 的要求，虽然实测
不影响识别）。

```bash
cd <model-dir>
mv ngram_table.bin ngram_table.bf16
mv ngram_table_4bit.bin ngram_table.bin
# config.json: "ngram_table": {"file": "ngram_table.bin", "bits": 4, "group_size": 32}
```

**验证**：启动日志会打印 `[qwen4] ngram table: 320001536 rows x 160 at 4 bits (29.8 GB)`。

## 4. `sushi kld: UnknownFlag`（传了 `--limit 0`）

**症状**：`kld capture --limit 0` 报 UnknownFlag，但帮助文本写着 "0 = all"。

**原因**：文档与实现不一致，`0` 没有走「全部」分支。

**解法**：不传 `--limit`（默认就是全部）。

## 5. `kld compare` 不支持 `--config-overrides`

**症状**：想用 `--config-overrides '{"ngram_table":...}'` 在对比时临时换表，报 UnknownFlag。

**原因**：`--config-overrides` 只存在于 `serve`/主命令，`kld` 子命令没实现。

**解法**：用 GOTCHAS #3 的物理换名法，或按 `scripts/run_kld_compare.sh` 那样
备份 config → 改写 → 测试 → 还原。

## 6. 与 mlx.serve 抢内存

**症状**：sushi 在跑时启动 mlx.serve，报 `InsufficientMemory`（或反之）。

**原因**：两者各需 63.7 GiB / 70.1 GiB 常驻，128 GB 机器装不下两个。

**解法**：先停掉一个。`pkill -f "sushi serve"` / `pkill -f "mlx-serve serve"`。
（顺带：即使加 `--skip-mem-preflight` 也不该硬来，两个 MoE 模型同时驻留会互相拖慢。）

## 7. 二进制下载被截断（`tar: truncated gzip input`）

**症状**：`curl` 下载 release 二进制后解压报截断，但 `sushi --version` 居然还能跑。

**原因**：网络中断导致 tar 包只下了一部分。主二进制恰好在截断点之前，所以能执行，
但 `lib/mlx.metallib` 等运行时文件缺失或不完整——这种「看起来能用」的状态最危险。

**解法**：**始终核对 release 提供的 sha256**。

```bash
curl -L -o sushi-bin.tar.gz.sha256 \
  https://github.com/beamivalice/sushi/releases/latest/download/sushi-bin-macos-arm64.tar.gz.sha256
shasum -a 256 -c sushi-bin.tar.gz.sha256
```

完整包约 67.7 MB（67712160 字节）。

## 8. `hf` CLI 配 hf-mirror 卡死

**症状**：`HF_ENDPOINT=https://hf-mirror.com hf download ...` 下载到几百 MB 后停滞，
进度条长时间不动；`.cache` 里多个 `.incomplete` 文件为 0 字节。

**原因**：`hf_transfer`（CLI 的默认加速器）与 hf-mirror 的 302 跳转/分块处理不兼容。
实测禁用 `HF_HUB_ENABLE_HF_TRANSFER=0` 后仍然不稳定。

**解法**：不要用 hf CLI 下载大包，改用 `scripts/dl_update.py`（curl 分段并行 + 断点续传）。

## 9. 脚本里 `$KV="--kv-quant 8"` 整串当成一个参数（zsh）

**症状**：命令单独在终端跑正常，放进脚本就报 UnknownFlag。

**原因**：**zsh 默认不对未加引号的变量做分词**（与 bash 不同）。`$KV` 展开成
单个参数 `--kv-quant 8`，而不是两个。

**解法**：zsh 里用 `${=KV}` 强制分词，或（更省心）直接把参数写全、别塞进变量。

```bash
# 会出错
KV="--kv-quant 8"; sushi kld compare ... $KV
# 可行
KV="--kv-quant 8"; sushi kld compare ... ${=KV}
```

## 10. GitHub 直连不稳定 / `Error in the HTTP2 framing layer`

**症状**：`git clone` 或 `curl` 访问 GitHub 有时几秒成功、有时超时几十秒，或报
`Error in the HTTP2 framing layer`。

**原因**：国内到 GitHub 的网络路径不稳定（不是代理本身的问题）。

**解法**：

- `curl` / 下载：走本地代理 `-x http://127.0.0.1:7897`（实测稳定 450 KB/s）。
- `git clone`：同样走代理；若报 HTTP/2 错误再加 `-c http.version=HTTP/1.1`。
  另外 `~/.gitconfig` 里的旧代理配置可能干扰，可用 `GIT_CONFIG_GLOBAL=/dev/null` 绕过。

## 11. `sushi serve` 日志显示监听 0.0.0.0

**症状**：mlx.serve 默认绑 `0.0.0.0`（对局域网开放），想只给本机。

**解法**：sushi 默认已是 `127.0.0.1`（比 mlx.serve 安全）；mlx.serve 需要显式加
`--host 127.0.0.1`。两者都建议显式指定，避免无意暴露到局域网。