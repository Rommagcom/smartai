from __future__ import annotations

import importlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DTYPE_MAP: dict[str, str] = {
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


def _load_pipeline(*, model_id: str, dtype: str, enable_model_cpu_offload: bool) -> tuple[Any, Any]:
    try:
        torch_module = importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        raise RuntimeError("torch is not installed. Install dependency: torch") from exc

    try:
        diffusers_module = importlib.import_module("diffusers")
    except ModuleNotFoundError as exc:
        raise RuntimeError("diffusers is not installed. Install dependency: diffusers") from exc

    try:
        transformers_module = importlib.import_module("transformers")
    except ModuleNotFoundError as exc:
        raise RuntimeError("transformers is not installed. Install dependency: transformers") from exc

    torch_dtype = _resolve_dtype(torch_module, dtype)

    cache_key = (model_id, str(torch_dtype))
    cached = _PIPELINE_CACHE.get(cache_key)
    if cached is not None:
        return cached, torch_module

    hf_token = os.getenv("HUGGINGFACE_HUB_TOKEN") or os.getenv("HF_TOKEN")

    wan_pipeline_cls = getattr(diffusers_module, "WanPipeline", None)
    wan_transformer_cls = getattr(diffusers_module, "WanTransformer3DModel", None)
    bitsandbytes_cfg_cls = getattr(transformers_module, "BitsAndBytesConfig", None)
    t5_encoder_cls = getattr(transformers_module, "T5EncoderModel", None)

    if None in (wan_pipeline_cls, wan_transformer_cls, bitsandbytes_cfg_cls, t5_encoder_cls):
        raise RuntimeError(
            "Unsupported runtime for Wan 2.1: required classes are missing "
            "(WanPipeline, WanTransformer3DModel, BitsAndBytesConfig, T5EncoderModel)"
        )

    bnb_config = bitsandbytes_cfg_cls(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch_dtype,
        bnb_4bit_quant_type="nf4",
    )

    transformer = wan_transformer_cls.from_pretrained(
        model_id,
        subfolder="transformer",
        quantization_config=bnb_config,
        torch_dtype=torch_dtype,
        token=hf_token,
    )

    text_encoder = None
    for text_subfolder in ("text_encoder", "text_encoder_2"):
        try:
            text_encoder = t5_encoder_cls.from_pretrained(
                model_id,
                subfolder=text_subfolder,
                quantization_config=bnb_config,
                torch_dtype=torch_dtype,
                token=hf_token,
            )
            break
        except Exception:
            text_encoder = None

    if text_encoder is None:
        raise RuntimeError("Failed to load T5 encoder from subfolder text_encoder or text_encoder_2")

    pipe = wan_pipeline_cls.from_pretrained(
        model_id,
        transformer=transformer,
        text_encoder=text_encoder,
        torch_dtype=torch_dtype,
        device_map="balanced",
        token=hf_token,
        low_cpu_mem_usage=True,
    )

    # VAE tiling lowers peak VRAM on long clips and high resolution.
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()

    if enable_model_cpu_offload and hasattr(pipe, "enable_model_cpu_offload"):
        pipe.enable_model_cpu_offload()

    _PIPELINE_CACHE[cache_key] = pipe
    return pipe, torch_module


def text_to_video(
    prompt: str,
    negative_prompt: str = "",
    model_id: str = "Wan-AI/Wan2.1-T2V-14B-Diffusers",
    width: int = 1280,
    height: int = 720,
    num_frames: int = 81,
    num_inference_steps: int = 40,
    guidance_scale: float = 6.0,
    fps: int = 16,
    seed: int | None = None,
    dtype: str = "bfloat16",
    filename: str | None = None,
    enable_model_cpu_offload: bool = False,
) -> str:
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

    pipeline, torch_module = _load_pipeline(
        model_id=model_id,
        dtype=dtype,
        enable_model_cpu_offload=bool(enable_model_cpu_offload),
    )

    generation_kwargs: dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": str(negative_prompt or "").strip(),
        "width": width,
        "height": height,
        "num_frames": int(num_frames),
        "num_inference_steps": int(num_inference_steps),
        "guidance_scale": float(guidance_scale),
    }
    if seed is not None:
        generator_device = "cuda" if bool(getattr(torch_module.cuda, "is_available", lambda: False)()) else "cpu"
        generation_kwargs["generator"] = torch_module.Generator(device=generator_device).manual_seed(int(seed))

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
        final_name = f"video_{stamp}.mp4"

    output_path = output_dir / final_name

    try:
        utils_module = importlib.import_module("diffusers.utils")
        export_to_video = getattr(utils_module, "export_to_video")
    except ModuleNotFoundError as exc:
        raise RuntimeError("diffusers.utils is unavailable. Install dependency: diffusers") from exc

    export_to_video(frames, str(output_path), fps=int(fps))

    payload: dict[str, object] = {
        "type": "video",
        "mime_type": "video/mp4",
        "filename": final_name,
        "path": str(output_path),
        "size_bytes": output_path.stat().st_size,
        "model_id": model_id,
        "width": width,
        "height": height,
        "num_frames": int(num_frames),
        "num_inference_steps": int(num_inference_steps),
        "guidance_scale": float(guidance_scale),
        "fps": int(fps),
        "seed": seed,
        "dtype": str(dtype),
        "enable_model_cpu_offload": bool(enable_model_cpu_offload),
    }

    return json.dumps(payload, ensure_ascii=True)
