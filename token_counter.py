"""Offline token counting with the model packs' own tokenizer and chat template."""

import json
import sys
import threading
from pathlib import Path


class TokenCounter:
    def __init__(self, packs_root, tools_dir, serve_dir):
        self.packs_root = Path(packs_root)
        for directory in (str(tools_dir), str(serve_dir)):
            if directory not in sys.path:
                sys.path.insert(0, directory)
        from frontend import anthropic_to_messages, openai_to_messages
        from strata_tokenizer import Tokenizer
        self._tokenizer_class = Tokenizer
        self._openai = openai_to_messages
        self._anthropic = anthropic_to_messages
        self.cache = {}
        self.lock = threading.RLock()

    def _load(self, model):
        if model in self.cache:
            return self.cache[model]
        pack = self.packs_root / model
        if not (pack / "tokenizer").is_dir():
            pack = self.packs_root / ("unsloth-" + model)
        root = pack / "tokenizer"
        raw = json.loads((root / "vocab.json").read_text(encoding="utf-8"))
        vocab = [token for token, _ in sorted(raw.items(), key=lambda item: item[1])]
        merges = (root / "merges.txt").read_text(encoding="utf-8").splitlines()
        token_types = json.loads((root / "token_type.json").read_text(encoding="utf-8"))
        meta = json.loads((root / "tokenizer.json").read_text(encoding="utf-8"))
        from frontend import ChatTemplate
        loaded = (
            self._tokenizer_class(vocab, merges, token_types, meta["pre"], meta["special_ids"]),
            ChatTemplate(root / "chat_template.jinja"),
        )
        self.cache[model] = loaded
        return loaded

    def count(self, model, data, path):
        tokenizer, template = self._load(model)
        if path == "/v1/chat/completions":
            messages, tools, kwargs = self._openai(data)
        elif path == "/v1/messages":
            messages, tools, kwargs = self._anthropic(data)
        else:
            raise ValueError(f"unsupported token count route: {path}")
        with self.lock:
            prompt = template.render(messages, tools=tools, **kwargs)
            prompt_tokens = len(tokenizer.encode(prompt, parse_special=True))
        return prompt_tokens + int(data.get("max_tokens") or 0)
