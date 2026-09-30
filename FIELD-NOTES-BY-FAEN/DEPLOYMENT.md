# 部署实录

从一台干净的 Mac 到能推理的完整过程。下文以 `SUSHI_HOME` 指代工作目录（本文用 `~/sushi`）。

## 1. 环境要求

| 项 | 要求 | 本文实测 |
|---|---|---|
| 芯片 | Apple Silicon（M1 及以上都能跑，M5 Pro/Max 有针对性优化） | Mac15,9 |
| 系统 | macOS ≥ 26.2 | 26.7 |
| 内存 | Sushi-4bpw 官方标称 96 GB 起；Sushi-3bpw 64 GB 起 | 128 GB |
| 磁盘 | 4bpw 包约 159 GiB（63.7 GiB 权重 + 95.4 GiB bf16 n-gram 表） | 190 GB（改用 4-bit 表后） |

内存核算（官方 README 的表格）：权重 63.68 GiB + MTP head 1.27 + 视觉塔 0.84 = 63.68 GiB 加载量；
再加 KV cache——256k 上下文 4.06 GiB、1M 上下文 16.25 GiB。128 GB 机器跑 1M 上下文总需求约 80 GiB。

## 2. 下载

### 2.1 二进制

```bash
mkdir -p ~/sushi && cd ~/sushi
curl -L -o sushi-bin.tar.gz \
  https://github.com/beamivalice/sushi/releases/latest/download/sushi-bin-macos-arm64.tar.gz
# 校验完整性（GitHub release 同时提供 .sha256，务必核对——见 GOTCHAS #7）
curl -L -o sushi-bin.tar.gz.sha256 \
  https://github.com/beamivalice/sushi/releases/latest/download/sushi-bin-macos-arm64.tar.gz.sha256
shasum -a 256 -c sushi-bin.tar.gz.sha256
tar xzf sushi-bin.tar.gz
```

**中国大陆网络提示**：GitHub 直连不稳（实测同一台机器上有时候几秒下完、有时候超时或
HTTP/2 framing 报错）。走本地代理稳定得多（实测 450 KB/s，67 MB 约 2.5 分钟）：

```bash
curl -L -x http://127.0.0.1:7897 -o sushi-bin.tar.gz <上面的 URL>
```

### 2.2 模型

**最快的路线：hf-mirror 直连 + 分段并行，实测 46 MB/s。** 对比：走代理直连 huggingface.co
只有 6 MB/s；ModelScope 没有收录这些社区量化包。

两个前置坑（详见 GOTCHAS #8、#9）：

- `hf` CLI 配 hf-mirror **会卡死**（`hf_transfer` 与镜像不兼容，下载中途停滞在几百 MB），
  禁用它改用普通下载器仍然不稳定。**不要用 hf CLI 下载这个包。**
- hf-mirror 对大文件会 302 到 `cas-bridge.xethub.hf.co`，而该 CDN 在国内可直连，
  所以用 `curl -L` 跟随跳转即可，无需代理。

推荐用本目录的脚本（`scripts/gen_manifest.py` + `scripts/dl_update.py`）：

```bash
# 1) 生成下载清单（排除不需要的 assets 图片）
python3 scripts/gen_manifest.py beamster/Qwen3.8-Flash-Next-Sushi-4bpw \
  --exclude 'assets/*' --out manifest.tsv

# 2) 分段并行下载（512 MB 分段、断点续传、精确大小校验、12 线程）
python3 scripts/dl_update.py beamster/Qwen3.8-Flash-Next-Sushi-4bpw \
  ~/sushi/models/Qwen3.8-Flash-Next-Sushi-4bpw manifest.tsv 12
```

**省 66 GiB 的技巧**：4bpw 包默认带 95.4 GiB 的 bf16 n-gram 表。可以改用 3bpw 包里的
4-bit 表（29.8 GiB），质量差异极小（见 [MEASUREMENTS.md](MEASUREMENTS.md)）：

```bash
python3 scripts/gen_manifest.py beamster/Qwen3.8-Flash-Next-Sushi-3bpw \
  --include 'ngram_table.bin' --out ngram4bit.tsv
python3 scripts/dl_update.py beamster/Qwen3.8-Flash-Next-Sushi-3bpw \
  ~/sushi/models/Qwen3.8-Flash-Next-Sushi-4bpw ngram4bit.tsv 12
# 下载后按 GOTCHAS #3 换成 4-bit 表并让 config.json 与之一致
```

### 2.3 目录布局与 `~/.sushi`

sushi 二进制把模型目录**硬编码**为 `~/.sushi/models`，且没有环境变量可以改
（二进制里只有 `SUSHI_ANE_CACHE_DIR` 等，没有 `SUSHI_MODELS_DIR`）。
所以 `sushi list` / `sushi pull` / `sushi run` 只认这个路径。

如果想像本文一样把数据放在别处（例如 `~/sushi`），最省事的办法是留一个软链接兜底：

```bash
mkdir -p ~/sushi && mv ~/.sushi/* ~/sushi/ && rmdir ~/.sushi
ln -s ~/sushi ~/.sushi
```

这样显式传 `--model` 时用真实路径，忘记传时 `list`/`pull` 也照常工作。

## 3. 启动

```bash
# 1) 提高 GPU 工作集上限（重启失效，需要 sudo）
sudo sysctl iogpu.wired_limit_mb=100000    # 128 GB 机器给 100 GB；96 GB 机器用 88000

# 2) 启动服务
~/sushi/sushi-macos-arm64/sushi serve \
  --model ~/sushi/models/Qwen3.8-Flash-Next-Sushi-4bpw \
  --mtp-head-kv-quant \
  --skip-mem-preflight \
  --port 12345
```

三个参数都有原因：

- `--model` 必须用**绝对路径**（相对路径会误报权重缺失，见 GOTCHAS #1）
- `--mtp-head-kv-quant` 把 MTP head 自己的 KV 也量化到 8 bit（省显存）
- `--skip-mem-preflight` 绕过内存预检（见 GOTCHAS #2）

**v1.0.5 起不要再用 `--mtp-typical`**：作者已从 README 删除该参数的说明段落，
改用新的默认 MTP 验证路径（v1.0.5 release notes 提到 "faster MTP verification"）。

## 4. 验证

```bash
# 服务就绪？（加载 63.7 GiB 权重约需 30–60 秒）
curl -s http://127.0.0.1:12345/v1/models | python3 -m json.tool | head -20

# 发一条请求
curl -s http://127.0.0.1:12345/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"用一句话说明什么是量化误差"}],"max_tokens":80}'
```

`/v1/models` 返回里几个值得看的字段：`state: ready`、`bytes_resident`（应约 68.4e9）、
`context_length`、`capabilities`（含 `chat`/`vision`/`reasoning`/`json_schema`）。

## 5. 接入客户端

服务是 **OpenAI 兼容**接口（`/v1/chat/completions`、`/v1/models`，流式和非流式都支持），
任意 OpenAI 客户端都能接。以 ZCode 为例，在 `~/.zcode/v2/provider_config.json` 的
`providerRules` 里加一条：

```json
{
  "providerId": "<任意 uuid>",
  "providerName": "sushi-local",
  "config": {
    "group": "standard-personal",
    "access": { "type": "api-key", "apiKey": "sk-local-no-key-needed" },
    "api": { "type": "openai-chat-completions", "baseUrl": "http://127.0.0.1:12345/v1" },
    "personalModelIds": ["Qwen3.8-Flash-Next-Sushi-4bpw"],
    "modelOrder": ["Qwen3.8-Flash-Next-Sushi-4bpw"]
  }
}
```

注意 sushi 本地服务不校验 API key（随便填）。服务没启动时选中该 provider 会连接失败。

## 6. 资源与共存

- **显存常驻**：服务启动后 63.7 GiB 权重常驻 GPU，不随请求释放。
- **不能与 mlx.serve 同时跑**：mlx.serve 的 mixed-4-8bit 包需要约 70 GiB，两者相加超过
  128 GB 机器可用量，后启动的一方会报 `InsufficientMemory`（实测）。要切换先
  `pkill -f "sushi serve"`。
- 停止服务：`pkill -f "sushi serve"`。