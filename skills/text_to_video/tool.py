from __future__ import annotations

import base64
import json
import re
import gc
import os
import importlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from diffusers import AutoencoderKLWan, WanPipeline
except ImportError:  # Backward compatibility for older diffusers builds.
    from diffusers import WanPipeline
    AutoencoderKLWan = None

from diffusers.utils import export_to_video


# ---------------------------- CONFIG ----------------------------
_DEFAULT_MODEL_ID = "Wan-AI/Wan2.1-T2V-14B-Diffusers"
_MODEL_ID_FALLBACKS = [
    "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
    "Wan-AI/Wan2.1-I2V-14B-720P-Diffusers",
]
_DEFAULT_WIDTH = 832
_DEFAULT_HEIGHT = 480
_DEFAULT_NUM_FRAMES = 81
_DEFAULT_NUM_INFERENCE_STEPS = 40
_DEFAULT_GUIDANCE_SCALE = 5.0
_DEFAULT_FPS = 25
_DEFAULT_NEGATIVE_PROMPT = (
    "blurry, low quality, distorted, static, text, watermark, shaky motion"
)

_PIPELINE_CACHE: dict[tuple[str, str, str], WanPipeline] = {}

# ---------------------------- HELPERS ----------------------------
def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    return cleaned or "video.mp4"


def _resolve_torch_dtype() -> Any:
    """Use bfloat16 by default as recommended in Wan examples."""
    torch_module = _get_torch_module()
    bf16 = getattr(torch_module, "bfloat16", None)
    if bf16 is not None:
        return bf16
    return torch_module.float16


def _get_torch_module() -> Any:
    try:
        return importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Torch is not installed in runtime environment. "
            "Install torch/torchvision/torchaudio in the bot container and rebuild image."
        ) from exc


def _select_cuda_device(torch_module: Any) -> str:
    if not torch_module.cuda.is_available():
        return "cpu"

    device_count = int(torch_module.cuda.device_count())
    if device_count <= 0:
        return "cpu"

    env_value = str(os.getenv("WAN_CUDA_DEVICE") or "").strip().lower()
    if env_value.startswith("cuda:"):
        env_value = env_value.split(":", 1)[1]
    if env_value.isdigit():
        env_index = int(env_value)
        if 0 <= env_index < device_count:
            return f"cuda:{env_index}"

    # Default preference: second GPU on multi-GPU hosts, else first GPU.
    preferred_index = 1 if device_count >= 2 else 0

    # If preferred GPU is tight on memory, pick the GPU with max free memory.
    try:
        free_list: list[tuple[int, int]] = []
        for idx in range(device_count):
            free_bytes, _ = torch_module.cuda.mem_get_info(idx)
            free_list.append((idx, int(free_bytes)))
        free_list.sort(key=lambda item: item[1], reverse=True)
        best_index = free_list[0][0]
        preferred_free = next((free for idx, free in free_list if idx == preferred_index), 0)
        best_free = free_list[0][1]
        # Use best GPU if preferred is heavily occupied.
        if best_index != preferred_index and best_free > preferred_free * 2:
            return f"cuda:{best_index}"
    except Exception:
        pass

    return f"cuda:{preferred_index}"

def _validated_dimension(value: int, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    if value % 16 != 0:
        raise ValueError(f"{field_name} must be a multiple of 16")
    return int(value)

# ---------------------------- PIPELINE LOADER ----------------------------
def _load_pipeline(*, model_id: str) -> WanPipeline:
    torch_module = _get_torch_module()
    preferred_dtype = _resolve_torch_dtype()
    target_device = _select_cuda_device(torch_module)

    hf_token = str(
        os.getenv("HUGGINGFACE_HUB_TOKEN")
        or os.getenv("HUGGINGFACE_TOKEN")
        or os.getenv("HF_TOKEN")
        or ""
    ).strip() or None

    model_ids = [model_id] + [candidate for candidate in _MODEL_ID_FALLBACKS if candidate != model_id]

    # Try robust combinations across model repos and diffusers/torch versions.
    last_exc: Exception | None = None
    pipe: WanPipeline | None = None
    selected_model_id = model_id

    for current_model_id in model_ids:
        cache_key = (current_model_id, str(preferred_dtype), target_device)
        if cache_key in _PIPELINE_CACHE:
            return _PIPELINE_CACHE[cache_key]

        try:
            vae = None
            if AutoencoderKLWan is not None:
                vae_kwargs: dict[str, Any] = {
                    "subfolder": "vae",
                    "torch_dtype": torch_module.float32,
                }
                if hf_token:
                    vae_kwargs["token"] = hf_token
                vae = AutoencoderKLWan.from_pretrained(current_model_id, **vae_kwargs)

            pipe_kwargs: dict[str, Any] = {
                "torch_dtype": preferred_dtype,
            }
            if vae is not None:
                pipe_kwargs["vae"] = vae
            if hf_token:
                pipe_kwargs["token"] = hf_token

            pipe = WanPipeline.from_pretrained(current_model_id, **pipe_kwargs)
            selected_model_id = current_model_id
            break
        except Exception as exc:
            last_exc = exc
            continue
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

    try:
        pipe.enable_xformers_memory_efficient_attention()
    except Exception:
        pass  # xformers optional

    # Run entire pipeline on selected single device to avoid cross-device scheduler issues.
    if target_device.startswith("cuda:"):
        try:
            torch_module.cuda.set_device(int(target_device.split(":", 1)[1]))
        except Exception:
            pass
        pipe = pipe.to(target_device)
        setattr(pipe, "_sai_device", target_device)

    _stabilize_scheduler(pipe)

    final_cache_key = (selected_model_id, str(preferred_dtype), target_device)
    _PIPELINE_CACHE[final_cache_key] = pipe
    return pipe


def _stabilize_scheduler(pipe: WanPipeline) -> None:
    scheduler = getattr(pipe, "scheduler", None)
    if scheduler is None:
        return

    cls_name = type(scheduler).__name__.lower()
    if "unipc" not in cls_name:
        return

    # Avoid multistep internal cat path that often mixes CPU/CUDA tensors.
    try:
        pipe.scheduler = type(scheduler).from_config(
            scheduler.config,
            solver_order=1,
            lower_order_final=True,
        )
    except TypeError:
        try:
            pipe.scheduler = type(scheduler).from_config(scheduler.config, solver_order=1)
        except Exception:
            pass
    except Exception:
        pass


def _sync_scheduler_to_device(pipe: WanPipeline, *, torch_module: Any, device: str, num_inference_steps: int) -> None:
    scheduler = getattr(pipe, "scheduler", None)
    if scheduler is None:
        return

    if hasattr(scheduler, "set_timesteps"):
        try:
            scheduler.set_timesteps(num_inference_steps, device=torch_module.device(device))
        except TypeError:
            try:
                scheduler.set_timesteps(num_inference_steps)
            except Exception:
                pass
        except Exception:
            pass

    if hasattr(scheduler, "model_outputs"):
        try:
            scheduler.model_outputs = []
        except Exception:
            pass

    for attr in ("timesteps", "sigmas"):
        value = getattr(scheduler, attr, None)
        if hasattr(value, "to"):
            try:
                setattr(scheduler, attr, value.to(device))
            except Exception:
                pass

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

    def _generate_with_pipeline(active_pipeline: WanPipeline):
        if torch_module.cuda.is_available():
            active_device = str(getattr(active_pipeline, "_sai_device", "cuda:0"))
            _sync_scheduler_to_device(
                active_pipeline,
                torch_module=torch_module,
                device=active_device,
                num_inference_steps=int(num_inference_steps),
            )
        with torch_module.inference_mode():
            return active_pipeline(
                prompt=prompt,
                negative_prompt=negative_prompt,
                width=width,
                height=height,
                num_frames=num_frames,
                num_inference_steps=num_inference_steps,
                guidance_scale=guidance_scale,
            ).frames[0]

    # ----------------- LOAD PIPELINE -----------------
    pipeline = _load_pipeline(model_id=model_id)

    # ----------------- GENERATE -----------------
    frames = _generate_with_pipeline(pipeline)

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
    video_bytes = out_path.read_bytes()

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
        "size_bytes": len(video_bytes),
        "base64": base64.b64encode(video_bytes).decode("ascii"),
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