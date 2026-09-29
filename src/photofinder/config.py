import os
import sys
from pathlib import Path

DEFAULT_DATA_ROOT = Path("/Volumes/Ext1TB/Projects/photo-finder/data")
DATA_ROOT = Path(os.environ.get("PHOTOFINDER_DATA_ROOT") or DEFAULT_DATA_ROOT)
MODELS_DIR = DEFAULT_DATA_ROOT / "models"
SIGLIP_REPO = "models--timm--ViT-B-16-SigLIP2"
SIGLIP_FILES = ("open_clip_model.safetensors", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json")


def require_mounted(root: Path | None = None):
    root = root or DATA_ROOT
    if not root.is_dir():
        sys.exit(f"{root} does not exist; is the external disk mounted?")


def setup_model_env(models_dir: Path = MODELS_DIR):
    env = {
        "HF_HOME": models_dir / "huggingface",
        "HF_HUB_CACHE": models_dir / "huggingface" / "hub",
        "TORCH_HOME": models_dir / "torch",
        "YOLO_CONFIG_DIR": models_dir / "ultralytics",
        "XDG_CACHE_HOME": models_dir / "cache",
        "MPLCONFIGDIR": models_dir / "matplotlib",
    }
    for key, path in env.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    if hf_cached(env["HF_HUB_CACHE"]):
        os.environ["HF_HUB_OFFLINE"] = "1"


def hf_cached(hub: Path) -> bool:
    return any(all((snap / f).is_file() for f in SIGLIP_FILES)
               for snap in (hub / SIGLIP_REPO / "snapshots").glob("*"))


def device() -> str:
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"
