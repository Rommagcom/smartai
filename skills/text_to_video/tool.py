from __future__ import annotations

import json
import re
import gc
import os
import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from diffusers import WanVideoPipeline
except ImportError:  # Backward compatibility for older diffusers builds.
    from diffusers import WanPipeline as WanVideoPipeline

from diffusers.utils import export_to_video


# ---------------------------- CONFIG ----------------------------
_DEFAULT_MODEL_ID = "Wan-AI/Wan2.1-T2V-14B-Diffusers"
_MODEL_ID_FALLBACKS = [
    "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
    "Wan-AI/Wan2.1-I2V-14B-720P-Diffusers",
]
_DEFAULT_WIDTH = 960
_DEFAULT_HEIGHT = 528
_DEFAULT_NUM_FRAMES = 72
_DEFAULT_NUM_INFERENCE_STEPS = 40
_DEFAULT_GUIDANCE_SCALE = 6.0
_DEFAULT_FPS = 25
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
    torch_module = _get_torch_module()
    fp8 = getattr(torch_module, "float8_e4m3fn", None)
    if fp8 is not None and torch_module.cuda.is_available():
        return fp8
    return torch_module.float16


def _get_torch_module() -> Any:
    try:
        return importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Torch is not installed in runtime environment. "
            "Install torch/torchvision/torchaudio in the bot container and rebuild image."
        ) from exc

def _validated_dimension(value: int, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    if value % 16 != 0:
        raise ValueError(f"{field_name} must be a multiple of 16")
    return int(value)

# ---------------------------- PIPELINE LOADER ----------------------------
def _load_pipeline(*, model_id: str) -> WanVideoPipeline:
    torch_module = _get_torch_module()
    preferred_dtype = _resolve_torch_dtype()
    dtype_candidates = [preferred_dtype]
    if preferred_dtype is not torch_module.float16:
        dtype_candidates.append(torch_module.float16)

    hf_token = str(
        os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HUGGINGFACE_TOKEN")
        or os.getenv("HF_TOKEN")
        or ""
    ).strip() or None

    model_ids = [model_id] + [candidate for candidate in _MODEL_ID_FALLBACKS if candidate != model_id]

    # Try robust combinations across model repos and diffusers/torch versions.
    last_exc: Exception | None = None
    pipe: WanVideoPipeline | None = None
    selected_dtype: Any | None = None
    selected_model_id = model_id
    for current_model_id in model_ids:
        for torch_dtype in dtype_candidates:
            cache_key = (current_model_id, str(torch_dtype), torch_module.cuda.device_count())
            if cache_key in _PIPELINE_CACHE:
                return _PIPELINE_CACHE[cache_key]

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

            for kwargs in candidates:
                if not hf_token and "token" in kwargs:
                    kwargs = dict(kwargs)
                    kwargs.pop("token", None)
                try:
                    pipe = WanVideoPipeline.from_pretrained(current_model_id, **kwargs)
                    selected_dtype = torch_dtype
                    selected_model_id = current_model_id
                    break
                except Exception as exc:
                    last_exc = exc
                    continue
            if pipe is not None:
                break
        if pipe is not None:
            break

    if pipe is None:
        msg = "Failed to initialize WanVideoPipeline"
        if hf_token is None:
            msg += ": set HF_TOKEN/HUGGINGFACE_TOKEN/HUGGINGFACE_HUB_TOKEN"
        if last_exc is not None:
            raise RuntimeError(f"{msg}. Last error: {last_exc}") from last_exc
        raise RuntimeError(msg)

    # ------- MEMORY‑SAVE SETTINGS -------
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        try:
            pipe.vae.enable_tiling(tile_size=256)
        except TypeError:
            # Backward-compatible path for diffusers builds without tile_size arg.
            pipe.vae.enable_tiling()

    # Do not enable CPU offload here: with some Wan scheduler/device_map combinations
    # it can produce mixed CPU/CUDA tensors during denoising steps.

    try:
        pipe.enable_xformers_memory_efficient_attention()
    except Exception:
        pass  # xformers optional

    final_cache_key = (selected_model_id, str(selected_dtype), torch_module.cuda.device_count())
    _PIPELINE_CACHE[final_cache_key] = pipe
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
    torch_module = _get_torch_module()

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
    with torch_module.inference_mode():
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
    if torch_module.cuda.is_available():
        torch_module.cuda.empty_cache()
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