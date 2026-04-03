from __future__ import annotations

import json
import re
import gc
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import torch
from torch.cuda.amp import autocast
from diffusers import WanPipeline
from diffusers.utils import export_to_video
# ---------------------------- CONFIG ----------------------------
_DEFAULT_IMAGE = "smartai_success.png"
_DEFAULT_MODEL_ID = "Wan-AI/Wan2.1-I2V-14B-720P-Diffusers"
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720
_DEFAULT_NUM_FRAMES = 75
_DEFAULT_NUM_INFERENCE_STEPS = 30   # <- lowered
_DEFAULT_GUIDANCE_SCALE = 6.0
_DEFAULT_FPS = 25
_DEFAULT_DTYPE = "float16"          # <- FP16 (lighter than bfloat16)
_DEFAULT_NEGATIVE_PROMPT = (
    "blurry, low quality, distorted, static, text, watermark, shaky motion"
)

_DTYPE_MAP = {"float16": "float16", "bfloat16": "bfloat16", "float32": "float32"}
_PIPELINE_CACHE: dict[tuple[str, str, int], WanPipeline] = {}

# ---------------------------- HELPERS ----------------------------
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

# ---------------------------- PIPELINE LOADER ----------------------------
def _load_pipeline(*, model_id: str, dtype: str) -> WanPipeline:
    torch_dtype = _resolve_dtype(torch, dtype)

    cache_key = (model_id, str(torch_dtype), torch.cuda.device_count())
    if cache_key in _PIPELINE_CACHE:
        return _PIPELINE_CACHE[cache_key]

    # Some diffusers versions don't support all kwargs; try the richest config first,
    # then gracefully degrade to compatible variants.
    candidates = [
        {
            "torch_dtype": torch_dtype,
            "device_map": "balanced",
            "low_cpu_mem_usage": True,
            "enable_model_parallelism": True,
        },
        {
            "torch_dtype": torch_dtype,
            "device_map": "balanced",
            "low_cpu_mem_usage": True,
        },
        {
            "torch_dtype": torch_dtype,
            "low_cpu_mem_usage": True,
        },
        {
            "torch_dtype": torch_dtype,
        },
    ]

    last_exc: Exception | None = None
    pipe: WanPipeline | None = None
    for kwargs in candidates:
        try:
            pipe = WanPipeline.from_pretrained(model_id, **kwargs)
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

    if hasattr(pipe, "enable_attention_slicing"):
        try:
            pipe.enable_attention_slicing(slice_size=2)
        except TypeError:
            pipe.enable_attention_slicing()

    try:
        pipe.enable_xformers_memory_efficient_attention()
    except Exception:
        pass  # xformers optional

    if hasattr(pipe.unet, "enable_gradient_checkpointing"):
        pipe.unet.enable_gradient_checkpointing()

    # Optional JIT compile (speed, not memory)
    try:
        pipe.unet = torch.compile(pipe.unet, mode="max-autotune")
        pipe.vae  = torch.compile(pipe.vae,  mode="max-autotune")
    except Exception as exc:
        print("⚠️ torch.compile not available:", exc)

    _PIPELINE_CACHE[cache_key] = pipe
    return pipe

# ---------------------------- CHUNKED GENERATION ----------------------------
def _run_pipeline(pipe, generation_kwargs):
    """Run a single diffusion call inside autocast + inference_mode."""
    with torch.inference_mode():
        if torch.cuda.is_available():
            with autocast("cuda", dtype=torch.float16):
                result = pipe(**generation_kwargs)
        else:
            result = pipe(**generation_kwargs)
    return result

def generate_frames_in_chunks(
    pipe,
    prompt: str,
    total_frames: int,
    chunk_size: int = 25,
    **generation_kwargs,
):
    """Generate `total_frames` frames by repeatedly calling the pipeline."""
    frames = []

    for start in range(0, total_frames, chunk_size):
        cur_len = min(chunk_size, total_frames - start)
        run_kwargs = dict(generation_kwargs)
        run_kwargs.update(
            {
                "prompt": prompt,
                "num_frames": cur_len,
            }
        )
        result = _run_pipeline(pipe, run_kwargs)
        frames.extend(result.frames[0])   # list of tensors (num_frames, H, W, 3)

    return frames

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
    dtype: str = _DEFAULT_DTYPE,
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
    pipeline = _load_pipeline(model_id=model_id, dtype=dtype)

    # ----------------- GENERATE -----------------
    generation_kwargs = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "num_inference_steps": num_inference_steps,
        "guidance_scale": guidance_scale,
        "height": height,
        "width": width,
        # `latent` will be added inside the chunked helper
    }

    # Use chunked generation to keep per‑GPU RAM low
    frames = generate_frames_in_chunks(
        pipeline,
        prompt,
        total_frames=num_frames,
        chunk_size=25,          # 25‑frame windows (≈ 1 s at 25 fps)
        **generation_kwargs,
    )

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
        "dtype": dtype,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)