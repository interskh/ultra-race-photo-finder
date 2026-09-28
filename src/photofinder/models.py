import gc
import io
import logging
import time

import numpy as np
from PIL import Image, ImageOps

from photofinder import config

YOLO_WEIGHTS = "yolo26s.pt"
OSNET_WEIGHTS = "osnet_x1_0_msmt17.pt"
SIGLIP = ("ViT-B-16-SigLIP2", "webli")
PERSON_CLASS, MIN_CONF, MIN_HEIGHT, IMGSZ = 0, 0.35, 96, 1280

log = logging.getLogger("models")
_loaded = {}
half_precision = False
loading = None


def _get(name, load):
    global loading
    if name not in _loaded:
        config.setup_model_env()
        t0 = time.monotonic()
        loading = name
        try:
            _loaded[name] = load()
        finally:
            loading = None
        log.info("loaded %s on %s in %.1fs", name, device(), time.monotonic() - t0)
    return _loaded[name]


def loaded() -> list[str]:
    return [k for k in list(_loaded) if k != "device"]


def device() -> str:
    if "device" not in _loaded:
        _loaded["device"] = config.device()
    return _loaded["device"]


def yolo():
    def load():
        from ultralytics import YOLO
        return YOLO(str(config.MODELS_DIR / YOLO_WEIGHTS))
    return _get("yolo", load)


def osnet():
    def load():
        from boxmot.reid.core.runtime import ReID
        return ReID(config.MODELS_DIR / "boxmot" / OSNET_WEIGHTS, device=device())
    return _get("osnet", load)


def siglip():
    def load():
        import open_clip
        precision = "pure_fp16" if half_precision and device() == "mps" else "fp32"
        model, _, preprocess = open_clip.create_model_and_transforms(*SIGLIP, device=device(), precision=precision)
        return model.eval(), preprocess
    return _get("siglip", load)


def tokenizer():
    def load():
        import open_clip
        return open_clip.get_tokenizer(SIGLIP[0], tokenizer_type="gemma")
    return _get("siglip_tokenizer", load)


def unload(*names):
    models = [k for k in _loaded if k != "device" and (not names or k in names)]
    for k in models:
        del _loaded[k]
    gc.collect()
    if models and device() == "mps":
        import torch
        torch.mps.empty_cache()


def load_image(path) -> Image.Image:
    with Image.open(path) as img:
        img.load()
        return ImageOps.exif_transpose(img).convert("RGB")


def filter_boxes(rows) -> list[tuple]:
    return [(float(x1), float(y1), float(x2), float(y2), float(conf))
            for x1, y1, x2, y2, conf, cls in rows
            if int(cls) == PERSON_CLASS and conf >= MIN_CONF and y2 - y1 >= MIN_HEIGHT]


def detect_persons(images: list[Image.Image]) -> list[list[tuple]]:
    results = yolo().predict(images, classes=[PERSON_CLASS], conf=MIN_CONF, imgsz=IMGSZ,
                             device=device(), verbose=False)
    return [filter_boxes(r.boxes.data.cpu().numpy()) for r in results]


def crop(img: Image.Image, box) -> Image.Image:
    x1, y1, x2, y2 = box
    w, h = img.size
    return img.crop((max(0, round(x1)), max(0, round(y1)), min(w, round(x2)), min(h, round(y2))))


def l2norm(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n > 0, n, 1)


def embed_crops(crops: list[Image.Image]) -> tuple[np.ndarray, np.ndarray]:
    import torch
    reid = osnet()([np.ascontiguousarray(np.asarray(c)[:, :, ::-1]) for c in crops])
    return l2norm(reid), embed_images(crops)


def embed_images(images: list[Image.Image]) -> np.ndarray:
    import torch
    model, preprocess = siglip()
    with torch.no_grad():
        batch = torch.stack([preprocess(img) for img in images]).to(device(), next(model.parameters()).dtype)
        return l2norm(model.encode_image(batch).float().cpu().numpy())


def text_request():
    def load():
        import Vision
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setRecognitionLanguages_(["en-US"])
        return req
    return _get("vision_text", load)


def read_text(img: Image.Image) -> list[tuple[str, float]]:
    import objc
    import Vision
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    with objc.autorelease_pool():
        req = text_request()
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(buf.getvalue(), None)
        ret = handler.performRequests_error_([req], None)
        ok, err = ret if isinstance(ret, tuple) else (bool(ret), None)
        if not ok or err is not None:
            raise RuntimeError(f"Vision text recognition failed: {err}")
        return [(str(r.text()), float(r.confidence())) for r in req.results() or []]


def encode_text(texts: list[str]) -> np.ndarray:
    import torch
    model, _ = siglip()
    with torch.no_grad():
        tokens = tokenizer()(list(texts)).to(device())
        return l2norm(model.encode_text(tokens).float().cpu().numpy())
