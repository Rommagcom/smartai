from __future__ import annotations

import importlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DTYPE_MAP: dict[str, str] = {
    "float8_e4m3fn": "float8_e4m3fn",
    "float16": "float16",
    "bfloat16": "bfloat16",
    "float32": "float32",
}

_PIPELINE_CACHE: dict[tuple[str, str], Any] = {}


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


def _load_pipeline(*, model_id: str, dtype: str, enable_model_cpu_offload: bool) -> tuple[Any, Any, Any]:
    try:
        torch_module = importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        raise RuntimeError("torch is not installed. Install dependency: torch") from exc

    try:
        diffusers_module = importlib.import_module("diffusers")
    except ModuleNotFoundError as exc:
        raise RuntimeError("diffusers is not installed. Install dependency: diffusers") from exc

    i2v_pipeline_cls = getattr(diffusers_module, "WanImageToVideoPipeline")
    torch_dtype = _resolve_dtype(torch_module, dtype)

    cache_key = (model_id, str(torch_dtype))
    cached = _PIPELINE_CACHE.get(cache_key)
    if cached is not None:
        utils_module = importlib.import_module("diffusers.utils")
        return cached, torch_module, utils_module

    hf_token = os.getenv("HUGGINGFACE_HUB_TOKEN") or os.getenv("HF_TOKEN")

    pipe = i2v_pipeline_cls.from_pretrained(
        model_id,
        torch_dtype=torch_dtype,
        token=hf_token,
        device_map="auto",
        low_cpu_mem_usage=True,
    )

    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()

    if enable_model_cpu_offload and hasattr(pipe, "enable_model_cpu_offload"):
        pipe.enable_model_cpu_offload()

    _PIPELINE_CACHE[cache_key] = pipe
    utils_module = importlib.import_module("diffusers.utils")
    return pipe, torch_module, utils_module


def image_to_video(
    image: str,
    prompt: str,
    negative_prompt: str = "",
    model_id: str = "Wan-AI/Wan2.1-I2V-14B-Diffusers",
    width: int = 1280,
    height: int = 720,
    num_frames: int = 81,
    num_inference_steps: int = 40,
    guidance_scale: float = 5.0,
    fps: int = 16,
    seed: int | None = None,
    dtype: str = "bfloat16",
    filename: str | None = None,
) -> str:
    image_ref = str(image or "").strip()
    if not image_ref:
        raise ValueError("image is required (path or URL)")

    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    width = _validated_dimension(int(width), "width")
    height = _validated_dimension(int(height), "height")

    if int(num_frames) <= 0:
        raise ValueError("num_frames must be greater than 0")
    if int(num_inference_steps) <= 0:
        raise ValueError("num_inference_steps must be greater than 0")
    if int(fps) <= 0:
        raise ValueError("fps must be greater than 0")

    pipeline, torch_module, utils_module = _load_pipeline(
        model_id=model_id,
        dtype=dtype,
        enable_model_cpu_offload=True,
    )

    load_image = getattr(utils_module, "load_image")
    source_image = load_image(image_ref).resize((width, height))

    generation_kwargs: dict[str, Any] = {
        "image": source_image,
        "prompt": prompt,
        "negative_prompt": str(negative_prompt or "").strip(),
        "num_frames": int(num_frames),
        "num_inference_steps": int(num_inference_steps),
        "guidance_scale": float(guidance_scale),
    }
    if seed is not None:
        generation_kwargs["generator"] = torch_module.Generator(device="cuda").manual_seed(int(seed))

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
        final_name = f"i2v_{stamp}.mp4"

    output_path = output_dir / final_name
    export_to_video = getattr(utils_module, "export_to_video")
    export_to_video(frames, str(output_path), fps=int(fps))

    payload: dict[str, object] = {
        "type": "video",
        "mode": "image_to_video",
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
        "enable_model_cpu_offload": True,
    }

    return json.dumps(payload, ensure_ascii=True)
