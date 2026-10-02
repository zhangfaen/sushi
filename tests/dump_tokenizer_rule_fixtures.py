#!/usr/bin/env python3
"""Hermetic BPE fixtures for the two supported Split grammars (tokenizers 0.22+).
No model weights or proprietary vocabulary: train tiny tokenizers on this corpus.
"""
import json
from pathlib import Path
from tokenizers import Tokenizer, Regex, models, pre_tokenizers, trainers, decoders
TEXTS = ["ไม่เป็นไร ไว้คราวหน้าก็ได้", "เข้าใจค่ะ แต่ยังไม่สามารถยืนยันวันศุกร์ได้ค่ะ", "ก้ก ก้้ก ้ก", "عَلَى عَيْنِي وَرَأْسِي", "زار موسى عيسى ١٢٣", "cafe\u0301 naïve déjà vu", "हिन्दी नमस्ते", "שלום שָׁלוֹם", "中文 日本語 한국어", "Русский Ελληνικά", "a×b÷c ½Ⅳ²", "I'm HERE!\r\n  café 12345", "x\u00a0\u2003y\u2028\n", "\u0301\u0301x !\u0301!\u0301", "a\u0301\u0301b"]
PREFIX = "(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\\r\\n\\p{L}\\p{N}]?"
TAIL = "|\\s*[\\r\\n]+|\\s+(?!\\S)|\\s+"
PATTERNS = {
    'letters': PREFIX + "\\p{L}+|\\p{N}| ?[^\\s\\p{L}\\p{N}]+[\\r\\n]*" + TAIL,
    'letters_marks': PREFIX + "[\\p{L}\\p{M}]+|\\p{N}| ?[^\\s\\p{L}\\p{M}\\p{N}]+[\\r\\n]*" + TAIL,
}
if __name__ == '__main__':
    result=[]
    for name,pattern in PATTERNS.items():
        tok=Tokenizer(models.BPE())
        tok.pre_tokenizer=pre_tokenizers.ByteLevel(add_prefix_space=False,use_regex=False)
        tok.decoder=decoders.ByteLevel()
        tok.train_from_iterator(TEXTS,trainers.BpeTrainer(vocab_size=1000,initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),show_progress=False))
        tok.pre_tokenizer=pre_tokenizers.Sequence([pre_tokenizers.Split(Regex(pattern),'isolated'),pre_tokenizers.ByteLevel(add_prefix_space=False,use_regex=False)])
        result.append({'rule':name,'tokenizer':json.loads(tok.to_str()),'cases':[{'text':t,'ids':tok.encode(t).ids} for t in TEXTS]})
    dest=Path(__file__).resolve().parents[1]/'src/fixtures/tokenizer-rules.json'
    dest.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
