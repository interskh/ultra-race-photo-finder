import os
import sys
from pathlib import Path

DATA_ROOT = Path("/Volumes/Ext1TB/Projects/photo-finder/data")
MODELS_DIR = DATA_ROOT / "models"


def require_mounted(root: Path = DATA_ROOT):
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


def device() -> str:
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"
