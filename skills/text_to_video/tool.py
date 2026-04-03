import os, json, re, gc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch
from diffusers import WanPipeline
from diffusers.utils import export_to_video
from tqdm.auto import tqdm   # прогресс‑бар

# --------------------------------------------------------------
# 1️⃣  Параметры по‑умолчанию (можно переопределять через env‑vars)
# --------------------------------------------------------------
_DEFAULT_IMAGE = "smartai_success.png"
_DEFAULT_MODEL_ID = "Wan-AI/Wan2.1-I2V-14B-720P-Diffusers"
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720
_DEFAULT_NUM_FRAMES = 101
_DEFAULT_NUM_INFERENCE_STEPS = 50
_DEFAULT_GUIDANCE_SCALE = 6.0
_DEFAULT_FPS = 25
_DEFAULT_DTYPE = "bfloat16"
_DEFAULT_NEGATIVE_PROMPT = (
    "blurry, low quality, distorted, static, text, watermark, shaky motion"
)

# --------------------------------------------------------------
# 2️⃣  Утилиты
# --------------------------------------------------------------
_DTYPE_MAP = {"float16": "float16", "bfloat16": "bfloat16", "float32": "float32"}
_PIPELINE_CACHE: dict[tuple[str, str, int], WanPipeline] = {}

def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    return cleaned or "video.mp4"

def _resolve_dtype(torch_mod: Any, dtype: str) -> Any:
    key = (dtype or "bfloat16").strip().lower()
    if key not in _DTYPE_MAP:
        raise ValueError(f"dtype must be one of: {', '.join(_DTYPE_MAP)}")
    return getattr(torch_mod, _DTYPE_MAP[key])

def _validated_dimension(value: int, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    if value % 16 != 0:
        raise ValueError(f"{field_name} must be a multiple of 16")
    return int(value)

def _load_pipeline(*, model_id: str, dtype: str) -> WanPipeline:
    torch_dtype = _resolve_dtype(torch, dtype)
    cache_key = (model_id, str(torch_dtype), torch.cuda.device_count())
    if cache_key in _PIPELINE_CACHE:
        return _PIPELINE_CACHE[cache_key]

    pipe = WanPipeline.from_pretrained(
        model_id,
        torch_dtype=torch_dtype,
        device_map="balanced",          # <-- автоматическое распределение
        low_cpu_mem_usage=True,
        enable_model_parallelism=True,  # <-- активирует модель‑параллелизм
    )

    # VAE‑tiling экономит VRAM
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()

    # Попытка ускорить JIT‑компиляцию (не критично, но полезно)
    try:
        pipe.unet = torch.compile(pipe.unet, mode="max-autotune")
        pipe.vae  = torch.compile(pipe.vae,  mode="max-autotune")
    except Exception as exc:
        print("⚠️ torch.compile не удалось:", exc)

    _PIPELINE_CACHE[cache_key] = pipe
    return pipe

# --------------------------------------------------------------
# 3️⃣  Основная функция генерации
# --------------------------------------------------------------
def text_to_video(
    prompt: str,
    filename: str | None = None,
    *,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
    num_frames: int = _DEFAULT_NUM_FRAMES,
    num_inference_steps: int = _DEFAULT_NUM_INFERENCE_STEPS,
    guidance_scale: float = _DEFAULT_GUIDANCE_SCALE,
    fps: int = _DEFAULT_FPS,
    dtype: str = _DEFAULT_DTYPE,
    negative_prompt: str = _DEFAULT_NEGATIVE_PROMPT,
    model_id: str = _DEFAULT_MODEL_ID,
) -> str:
    # ----------------------- 1️⃣ Проверка входов -----------------------
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    width = _validated_dimension(width, "width")
    height = _validated_dimension(height, "height")
    if num_frames <= 0 or num_inference_steps <= 0 or fps <= 0:
        raise ValueError("num_frames / num_inference_steps / fps must be > 0")

    # ----------------------- 2️⃣ Загрузка пайплайна --------------------
    pipeline = _load_pipeline(model_id=model_id, dtype=dtype)

    # ----------------------- 3️⃣ Генерация ---------------------------
    generation_kwargs = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "num_frames": num_frames,
        "num_inference_steps": num_inference_steps,
        "guidance_scale": guidance_scale,
        "height": height,
        "width": width,
    }

    # Прогресс‑бар для инференса
    with torch.inference_mode():
        # Если вы хотите видеть прогресс в самом diffusers, используйте:
        #   pipeline.set_progress_bar_config(disable=False)
        # но ниже мы просто оборачиваем весь вызов в tqdm.
        for _ in tqdm(range(1), desc="Generating video", unit="step"):
            result = pipeline(**generation_kwargs)

    frames = result.frames[0]   # shape: (num_frames, H, W, 3)

    # ----------------------- 4️⃣ Сохранение --------------------------
    out_dir = Path("generated_videos")
    out_dir.mkdir(parents=True, exist_ok=True)

    if filename and filename.strip():
        final_name = _safe_filename(filename)
        if not final_name.lower().endswith(".mp4"):
            final_name += ".mp4"
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        final_name = f"smartai_6000_pro_{fps}fps_{stamp}.mp4"

    out_path = out_dir / final_name
    export_to_video(frames, str(out_path), fps=int(fps))

    # ----------------------- 5️⃣ Очистка VRAM -----------------------
    del pipeline, result, frames
    torch.cuda.empty_cache()
    gc.collect()

    # ----------------------- 6️⃣ Возврат метаданных -----------------
    payload = {
        "type": "video",
        "mode": "text_to_video",
        "mime_type": "video/mp4",
        "filename": final_name,
        "path": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "model_id": model_id,
        "source_image": _DEFAULT_IMAGE,
        "width": width,
        "height": height,
        "num_frames": num_frames,
        "num_inference_steps": num_inference_steps,
        "guidance_scale": guidance_scale,
        "fps": fps,
        "dtype": dtype,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)