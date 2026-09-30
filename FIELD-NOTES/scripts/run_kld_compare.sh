#!/bin/zsh
# 对比两个 n-gram 表的输出质量（KLD / top-1 / NLL）。
#
# 背景 (Why): 模型包默认带 95.4 GiB 的 bf16 n-gram 表，而 3bpw 包带同表的 4-bit 版
# （29.8 GiB）。本脚本用 bf16 表版本做 teacher（capture 全词表 logits），
# 再让 4-bit 表版本 teacher-force 一遍，KLD 直接量化两表差异——比引用别人
# 的数字更可信（你自己机器、你自己的提示词）。
#
# 切表方式 (What): 实测 sushi 只认文件名 ngram_table.bin，改 config.json 的
# ngram_table.file 指向别的文件名不生效（bits/group_size 从文件内嵌 meta 自动识别）。
# 所以本脚本用物理换名，并在退出时还原。
#
# 注意: 本脚本用 zsh。zsh 不对未加引号的变量做分词（与 bash 不同），
# 所以下面所有 sushi 参数都直接写全，不塞进变量——否则 "--kv-quant 8"
# 会变成单个参数导致 UnknownFlag。
#
# 用法:
#   run_kld_compare.sh <model-dir> [prompts] [work-dir]
#     model-dir  模型目录（必须已含 ngram_table.bin 与 ngram_table_4bit.bin）
#     prompts    提示词来源（*.txt 目录 或 .jsonl），默认 ../prompts.jsonl
#     work-dir   fixture 与结果输出目录，默认 ./kld-work
#   SUSHI 环境变量可指定 sushi 可执行文件，默认 "sushi"（需在 PATH 中）
set -e

SUSHI="${SUSHI:-sushi}"
MODEL="${1:?用法: run_kld_compare.sh <model-dir> [prompts] [work-dir]}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROMPTS="${2:-$SCRIPT_DIR/../prompts.jsonl}"
WORK="${3:-./kld-work}"
FIX="$WORK/fixture"
OUT="$WORK/results"
mkdir -p "$OUT"

if [ ! -f "$MODEL/ngram_table_4bit.bin" ]; then
  echo "错误: $MODEL 下没有 ngram_table_4bit.bin（4-bit 表）" >&2
  echo "先下载: python3 $SCRIPT_DIR/gen_manifest.py <3bpw-repo> --include 'ngram_table.bin' --out t.tsv" >&2
  echo "        python3 $SCRIPT_DIR/dl_update.py <3bpw-repo> $MODEL t.tsv" >&2
  echo "再改名为 ngram_table_4bit.bin" >&2
  exit 1
fi

# 退出时确保 bf16 表回到 ngram_table.bin 位置
restore() {
  if [ -f "$MODEL/ngram_table.bin.q4" ]; then
    mv -f "$MODEL/ngram_table.bin.q4" "$MODEL/ngram_table_4bit.bin"
    mv -f "$MODEL/ngram_table.bin.bf16" "$MODEL/ngram_table.bin"
  fi
}
trap restore EXIT

echo "=== [1/3] capture: bf16 表做 teacher ==="
"$SUSHI" kld capture --model "$MODEL" --prompts "$PROMPTS" \
  --out "$FIX" --kv-quant 8 --tokens 512 --label bf16-table

echo "=== [2/3] 换名到 4-bit 表, teacher-force 对比 ==="
mv "$MODEL/ngram_table.bin" "$MODEL/ngram_table.bin.bf16"
mv "$MODEL/ngram_table_4bit.bin" "$MODEL/ngram_table.bin.q4"
mv "$MODEL/ngram_table.bin.q4" "$MODEL/ngram_table.bin"
"$SUSHI" kld compare --model "$MODEL" --fixture "$FIX" \
  --kv-quant 8 --label 4bit-table --json "$OUT/4bit_vs_bf16.json"

echo "=== [3/3] 还原 bf16 表, sanity 自比（应接近 0）==="
restore
trap - EXIT
"$SUSHI" kld compare --model "$MODEL" --fixture "$FIX" \
  --kv-quant 8 --label bf16-self --json "$OUT/bf16_self.json"

echo
echo "=== 结果 ==="
for f in "$OUT/4bit_vs_bf16.json" "$OUT/bf16_self.json"; do
  echo "--- $f ---"
  cat "$f"
  echo
done