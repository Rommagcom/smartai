from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch
from diffusers import WanPipeline
from diffusers.utils import export_to_video

_DTYPE_MAP: dict[str, str] = {
    "float16": "float16",
    "bfloat16": "bfloat16",
    "float32": "float32",
}

_PIPELINE_CACHE: dict[tuple[str, str], Any] = {}

_DEFAULT_IMAGE = "smartai_success.png"
_DEFAULT_MODEL_ID = "Wan-AI/Wan2.1-I2V-14B-720P-Diffusers"
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720
_DEFAULT_NUM_FRAMES = 101
_DEFAULT_NUM_INFERENCE_STEPS = 50
_DEFAULT_GUIDANCE_SCALE = 6.0
_DEFAULT_FPS = 25
_DEFAULT_DTYPE = "bfloat16"
_DEFAULT_NEGATIVE_PROMPT = "blurry, low quality, distorted, static, text, watermark, shaky motion"


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    return cleaned or "video.mp4"


def _resolve_dtype(torch_module: Any, dtype: str) -> Any:
    key = (dtype or "bfloat16").strip().lower()
    if key not in _DTYPE_MAP:
        allowed = ", ".join(sorted(_DTYPE_MAP))
        raise ValueError(f"dtype must be one of: {allowed}")

    value = getattr(torch_module, _DTYPE_MAP[key], None)
    if value is None:
        raise RuntimeError(f"Torch dtype is not supported in this build: {key}")
    return value


def _validated_dimension(value: int, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    if value % 16 != 0:
        raise ValueError(f"{field_name} must be a multiple of 16")
    return int(value)


def _validate_generation_inputs(*, prompt: str, width: int, height: int, num_frames: int, num_inference_steps: int, fps: int) -> None:
    if not prompt:
        raise ValueError("prompt is required")
    if int(width) <= 0 or int(height) <= 0:
        raise ValueError("width and height must be greater than 0")
    if int(num_frames) <= 0:
        raise ValueError("num_frames must be greater than 0")
    if int(num_inference_steps) <= 0:
        raise ValueError("num_inference_steps must be greater than 0")
    if int(fps) <= 0:
        raise ValueError("fps must be greater than 0")


def _load_pipeline(*, model_id: str, dtype: str) -> tuple[Any, Any]:
    torch_dtype = _resolve_dtype(torch, dtype)

    cache_key = (model_id, str(torch_dtype))
    cached = _PIPELINE_CACHE.get(cache_key)
    if cached is not None:
        return cached, torch

    pipe = WanPipeline.from_pretrained(
        model_id,
        torch_dtype=torch_dtype,
        device_map="balanced",
        low_cpu_mem_usage=True
    )

    # VAE tiling lowers peak VRAM on long clips and high resolution.
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()

    _PIPELINE_CACHE[cache_key] = pipe
    return pipe, torch


def text_to_video(
    prompt: str,
    filename: str | None = None,
) -> str:
    prompt = str(prompt or "").strip()

    width = _validated_dimension(int(_DEFAULT_WIDTH), "width")
    height = _validated_dimension(int(_DEFAULT_HEIGHT), "height")
    num_frames = int(_DEFAULT_NUM_FRAMES)
    num_inference_steps = int(_DEFAULT_NUM_INFERENCE_STEPS)
    guidance_scale = float(_DEFAULT_GUIDANCE_SCALE)
    fps = int(_DEFAULT_FPS)
    seed = None
    dtype = _DEFAULT_DTYPE
    image_ref = _DEFAULT_IMAGE
    model_id = _DEFAULT_MODEL_ID
    negative_prompt = _DEFAULT_NEGATIVE_PROMPT

    _validate_generation_inputs(
        prompt=prompt,
        width=width,
        height=height,
        num_frames=num_frames,
        num_inference_steps=num_inference_steps,
        fps=fps,
    )

    pipeline, torch_module = _load_pipeline(
        model_id=model_id,
        dtype=dtype,
    )



    generation_kwargs: dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "num_frames": num_frames,
        "num_inference_steps": num_inference_steps,
        "guidance_scale": guidance_scale,
    }

    with torch_module.inference_mode():
        result = pipeline(**generation_kwargs)

    frames = result.frames[0]

    output_dir = Path("generated_videos")
    output_dir.mkdir(parents=True, exist_ok=True)

    if filename and filename.strip():
        final_name = _safe_filename(filename)
        if not final_name.lower().endswith(".mp4"):
            final_name += ".mp4"
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        final_name = f"smartai_6000_pro_25fps_{stamp}.mp4"

    output_path = output_dir / final_name

    export_to_video(frames, str(output_path), fps=int(fps))

    payload: dict[str, object] = {
        "type": "video",
        "mode": "text_to_video",
        "mime_type": "video/mp4",
        "filename": final_name,
        "path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "model_id": model_id,
        "source_image": image_ref,
        "width": width,
        "height": height,
        "num_frames": int(num_frames),
        "num_inference_steps": int(num_inference_steps),
        "guidance_scale": float(guidance_scale),
        "fps": int(fps),
        "seed": seed,
        "dtype": str(dtype),
    }

    return json.dumps(payload, ensure_ascii=True)
