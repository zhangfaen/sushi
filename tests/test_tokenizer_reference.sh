#!/bin/bash
# CPU-only real-pack parity. No server restart or model weights needed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODEL_ROOT="${SUSHI_MODELS_DIR:-$HOME/.sushi/models}"
FIXTURE="$(mktemp -t sushi-tokenizer-parity)"
trap 'rm -f "$FIXTURE"' EXIT
python3 - "$ROOT" "$MODEL_ROOT" "$FIXTURE" <<'PY'
import sys,json,os
from pathlib import Path
from tokenizers import Tokenizer
root,model_root,fixture=map(Path,sys.argv[1:])
sys.path.insert(0,str(root/'tests'))
from dump_tokenizer_rule_fixtures import TEXTS
texts=list(TEXTS)
if os.environ.get('TOKENIZER_CASES_JSON'):
    texts += [r['prompt'] for r in json.loads(Path(os.environ['TOKENIZER_CASES_JSON']).read_text())['cases']]
rows=[]
for name in ['Qwen3.8-Flash-Next-Sushi-4bpw','MiMo-V2.6-Flash-Sushi-2.3bpw']:
    path=model_root/name
    tok=Tokenizer.from_file(str(path/'tokenizer.json'))
    # Isolate Split/BPE parity from the separate, pre-existing NFC-normalizer gap.
    # Original benchmark prompts are already NFC; decomposed synthetic cases are normalized.
    normalized=[tok.normalizer.normalize_str(t) if tok.normalizer else t for t in texts]
    if os.environ.get('TOKENIZER_CASES_JSON'):
        assert normalized[len(TEXTS):] == texts[len(TEXTS):], 'Benchmark contains non-normalized prompts'
    rows.append({'model_dir':str(path.resolve()),'cases':[{'text':t,'ids':tok.encode(t,add_special_tokens=False).ids} for t in normalized]})
fixture.write_text(json.dumps(rows,ensure_ascii=False))
print(f'Checking {len(texts)} texts per model against Hugging Face tokenizers')
PY
cd "$ROOT"
SUSHI_TOKENIZER_PARITY_FIXTURE="$FIXTURE" "${ZIG:-$ROOT/.zig-toolchain/zig}" build test -Doptimize=ReleaseFast -Dtest-filter='real tokenizer reference parity'
