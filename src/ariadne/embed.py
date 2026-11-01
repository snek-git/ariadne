"""Qwen3-VL-Embedding-2B: text and pictures in one 2048-d space, so a text query finds both.
The loading tricks come from comfy-phone's tools/embed_index.py (the checkpoint's own wrapper,
the patch Conv3d as a matmul so ROCm stops re-tuning it per picture size)."""
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("HIP_VISIBLE_DEVICES", "0")  # the 9070 XT; 1 is the iGPU
os.environ.setdefault("HF_HOME", "/mnt/models/hf-cache")

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from transformers.models.qwen3_vl.modeling_qwen3_vl import Qwen3VLConfig, Qwen3VLModel, Qwen3VLPreTrainedModel
from transformers.models.qwen3_vl.processing_qwen3_vl import Qwen3VLProcessor

REPO = "Qwen/Qwen3-VL-Embedding-2B"
DIM = 2048
DOC = "Represent the user's input."
QUERY = "Retrieve images or text relevant to the user's query."
TILE = 480
TOKEN_BUDGET = 8192  # padded tokens per text batch
MAX_BATCH = 32
VRAM_USED = Path("/sys/class/drm/card1/device/mem_info_vram_used")
VRAM_TOTAL = Path("/sys/class/drm/card1/device/mem_info_vram_total")
VRAM_HEADROOM = int(0.6 * 2**30)  # Hyprland crashed when the card filled up (2026-10-11)


class Qwen3VLForEmbedding(Qwen3VLPreTrainedModel):
    _checkpoint_conversion_mapping = {}
    accepts_loss_kwargs = False
    config: Qwen3VLConfig

    def __init__(self, config):
        super().__init__(config)
        self.model = Qwen3VLModel(config)
        self.post_init()

    def forward(self, **inputs):
        return self.model(**inputs).last_hidden_state


class PatchLinear(torch.nn.Module):
    def __init__(self, conv: torch.nn.Conv3d):
        super().__init__()
        self.w = torch.nn.Parameter(conv.weight.data.reshape(conv.out_channels, -1), requires_grad=False)
        self.b = conv.bias
        self.weight = self.w

    def forward(self, x):
        return F.linear(x.reshape(x.shape[0], -1), self.w, self.b)


def vram_guard() -> None:
    """End the process if the whole card gets within VRAM_HEADROOM of full, whoever fills it."""
    if not VRAM_TOTAL.exists():
        return
    total = int(VRAM_TOTAL.read_text())

    def watch():
        while True:
            used = int(VRAM_USED.read_text())
            if total - used < VRAM_HEADROOM:
                print(f"VRAM guard: card at {used / 2**30:.2f}/{total / 2**30:.2f}G, stopping", file=sys.stderr, flush=True)
                os._exit(3)
            time.sleep(0.05)
    threading.Thread(target=watch, daemon=True).start()


def tile(im: Image.Image) -> Image.Image:
    im = im.convert("RGB")
    im.thumbnail((TILE, TILE))
    return im


class Embedder:
    def __init__(self, device: str = "cuda"):
        self.device = device if torch.cuda.is_available() else "cpu"
        if self.device == "cuda":
            vram_guard()
        self.model = Qwen3VLForEmbedding.from_pretrained(REPO, dtype=torch.bfloat16).eval()
        self.model.model.visual.patch_embed.proj = PatchLinear(self.model.model.visual.patch_embed.proj)
        self.model.to(self.device)
        self.proc = Qwen3VLProcessor.from_pretrained(REPO, padding_side="right")
        self.lock = threading.Lock()

    @torch.no_grad()
    def _run(self, convs: list, images: list[Image.Image] | None) -> np.ndarray:
        with self.lock:  # the fast tokenizer is not thread-safe, and the file manager searches per keystroke
            text = self.proc.apply_chat_template(convs, add_generation_prompt=True, tokenize=False)
            x = self.proc(text=text, images=images, padding=True, return_tensors="pt").to(self.device)
            h = self.model(**x)
            mask = x["attention_mask"]
            last = mask.shape[1] - 1 - mask.flip(dims=[1]).argmax(dim=1)
            e = h[torch.arange(h.shape[0], device=h.device), last]
            return F.normalize(e.float(), dim=-1).cpu().numpy()

    def texts(self, texts: list[str], query: bool = False) -> np.ndarray:
        """Any number of texts, batched so padded tokens stay under TOKEN_BUDGET: 16 chunks of
        3k tokens each filled the card (2026-10-29), a batch of short names costs nothing."""
        inst = QUERY if query else DOC
        texts = [t.encode("utf-8", "replace").decode() for t in texts]  # lone surrogates break the tokenizer
        convs = [[{"role": "system", "content": [{"type": "text", "text": inst}]},
                  {"role": "user", "content": [{"type": "text", "text": t}]}] for t in texts]
        with self.lock:
            lens = [len(ids) for ids in self.proc.tokenizer(
                self.proc.apply_chat_template(convs, add_generation_prompt=True, tokenize=False))["input_ids"]]
        order = sorted(range(len(texts)), key=lambda i: lens[i])
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        b: list[int] = []
        for i in order + [None]:
            if b and (i is None or len(b) == MAX_BATCH or lens[i] * (len(b) + 1) > TOKEN_BUDGET):
                out[b] = self._run([convs[j] for j in b], None)
                b = []
            if i is not None:
                b.append(i)
        return out

    def images(self, ims: list[Image.Image]) -> np.ndarray:
        convs = [[{"role": "system", "content": [{"type": "text", "text": DOC}]},
                  {"role": "user", "content": [{"type": "image"}]}] for _ in ims]
        return self._run(convs, [tile(im) for im in ims])
