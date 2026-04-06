from __future__ import annotations

import json
import re
import gc
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch

try:
    from diffusers import WanVideoPipeline
except ImportError:  # Backward compatibility for older diffusers builds.
    from diffusers import WanPipeline as WanVideoPipeline

from diffusers.utils import export_to_video


# ---------------------------- CONFIG ----------------------------
_DEFAULT_IMAGE = "smartai_success.png"
_DEFAULT_MODEL_ID = "Wan-AI/Wan2.1-T2V-14B-720P-Diffusers"
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720
_DEFAULT_NUM_FRAMES = 81
_DEFAULT_NUM_INFERENCE_STEPS = 40
_DEFAULT_GUIDANCE_SCALE = 6.0
_DEFAULT_FPS = 16
_DEFAULT_NEGATIVE_PROMPT = (
    "blurry, low quality, distorted, static, text, watermark, shaky motion"
)

_PIPELINE_CACHE: dict[tuple[str, str, int], WanVideoPipeline] = {}

# ---------------------------- HELPERS ----------------------------
def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    return cleaned or "video.mp4"


def _resolve_torch_dtype() -> Any:
    """Prefer FP8 on supported GPUs, fallback to FP16 for compatibility."""
    fp8 = getattr(torch, "float8_e4m3fn", None)
    if fp8 is not None and torch.cuda.is_available():
        return fp8
    return torch.float16

def _validated_dimension(value: int, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    if value % 16 != 0:
        raise ValueError(f"{field_name} must be a multiple of 16")
    return int(value)

# ---------------------------- PIPELINE LOADER ----------------------------
def _load_pipeline(*, model_id: str) -> WanVideoPipeline:
    torch_dtype = _resolve_torch_dtype()

    cache_key = (model_id, str(torch_dtype), torch.cuda.device_count())
    if cache_key in _PIPELINE_CACHE:
        return _PIPELINE_CACHE[cache_key]

    hf_token = str(os.getenv("HUGGINGFACE_HUB_TOKEN")
        or ""
    ).strip() or None

    # Try the recommended T2V loading options first, then degrade for older versions.
    candidates = [
        {
            "torch_dtype": torch_dtype,
            "token": hf_token,
            "device_map": "auto",
            "low_cpu_mem_usage": True,
        },
        {
            "torch_dtype": torch_dtype,
            "token": hf_token,
            "device_map": "auto",
            "low_cpu_mem_usage": True,
        },
        {
            "torch_dtype": torch_dtype,
            "token": hf_token,
            "low_cpu_mem_usage": True,
        },
        {
            "torch_dtype": torch_dtype,
            "token": hf_token,
        },
    ]

    last_exc: Exception | None = None
    pipe: WanVideoPipeline | None = None
    for kwargs in candidates:
        if not hf_token and "token" in kwargs:
            kwargs = dict(kwargs)
            kwargs.pop("token", None)
        try:
            pipe = WanVideoPipeline.from_pretrained(model_id, **kwargs)
            break
        except TypeError as exc:
            last_exc = exc
            continue

    if pipe is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Failed to initialize WanPipeline")

    # ------- MEMORY‑SAVE SETTINGS -------
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        try:
            pipe.vae.enable_tiling(tile_size=256)
        except TypeError:
            # Backward-compatible path for diffusers builds without tile_size arg.
            pipe.vae.enable_tiling()

    if hasattr(pipe, "enable_model_cpu_offload"):
        try:
            pipe.enable_model_cpu_offload()
        except Exception:
            pass

    try:
        pipe.enable_xformers_memory_efficient_attention()
    except Exception:
        pass  # xformers optional

    _PIPELINE_CACHE[cache_key] = pipe
    return pipe

# ---------------------------- MAIN FUNCTION ----------------------------
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
    dtype: str = "auto",
    negative_prompt: str = _DEFAULT_NEGATIVE_PROMPT,
    model_id: str = _DEFAULT_MODEL_ID,
) -> str:
    # ----------------- VALIDATE INPUTS -----------------
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    model_id = str(model_id or _DEFAULT_MODEL_ID).strip() or _DEFAULT_MODEL_ID

    width = _validated_dimension(width, "width")
    height = _validated_dimension(height, "height")
    if num_frames <= 0 or num_inference_steps <= 0 or fps <= 0:
        raise ValueError("num_frames / num_inference_steps / fps must be > 0")

    # ----------------- LOAD PIPELINE -----------------
    pipeline = _load_pipeline(model_id=model_id)

    # ----------------- GENERATE -----------------
    with torch.inference_mode():
        frames = pipeline(
            prompt=prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            num_frames=num_frames,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
        ).frames[0]

    # ----------------- SAVE VIDEO -----------------
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

    # ----------------- CLEAN‑UP -----------------
    del pipeline, frames
    torch.cuda.empty_cache()
    gc.collect()

    # ----------------- RETURN METADATA -----------------
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
        "dtype": str(_resolve_torch_dtype()),
        "prompt": prompt,
        "negative_prompt": negative_prompt,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)