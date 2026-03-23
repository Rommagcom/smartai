from __future__ import annotations

import base64
import importlib
import json
import os
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

_DTYPE_MAP: dict[str, str] = {
    "float16": "float16",
    "bfloat16": "bfloat16",
    "float32": "float32",
}

_PIPELINE_CACHE: dict[tuple[str, str, str], Any] = {}


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    return cleaned or "image.png"


def _resolve_dtype(torch_module: Any, dtype: str) -> Any:
    key = (dtype or "bfloat16").strip().lower()
    if key not in _DTYPE_MAP:
        allowed = ", ".join(sorted(_DTYPE_MAP))
        raise ValueError(f"dtype must be one of: {allowed}")
    return getattr(torch_module, _DTYPE_MAP[key])


def _validated_dimension(value: int, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    if value % 8 != 0:
        raise ValueError(f"{field_name} must be a multiple of 8")
    return int(value)


def _load_pipeline(*, model_id: str, dtype: str, device: str) -> tuple[Any, Any]:
    try:
        torch_module = importlib.import_module("torch")
    except ModuleNotFoundError as exc:
        raise RuntimeError("torch is not installed. Install dependency: torch") from exc

    try:
        diffusers_module = importlib.import_module("diffusers")
    except ModuleNotFoundError as exc:
        raise RuntimeError("diffusers is not installed. Install dependency: diffusers") from exc

    FluxPipeline = getattr(diffusers_module, "FluxPipeline")
    FluxTransformer2DModel = getattr(diffusers_module, "FluxTransformer2DModel")
    torch_dtype = _resolve_dtype(torch_module, dtype)

    cache_key = (model_id, str(torch_dtype), device)
    cached = _PIPELINE_CACHE.get(cache_key)
    if cached is not None:
        return cached, torch_module

    try:
        transformers_module = importlib.import_module("transformers")
    except ModuleNotFoundError as exc:
        raise RuntimeError("transformers is not installed. Install dependency: transformers") from exc

    BitsAndBytesConfig = getattr(transformers_module, "BitsAndBytesConfig")
    T5EncoderModel = getattr(transformers_module, "T5EncoderModel")

    hf_token = os.getenv("HUGGINGFACE_HUB_TOKEN") or os.getenv("HF_TOKEN")

    quant_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch_dtype,
        bnb_4bit_quant_type="nf4",
    )

    transformer = FluxTransformer2DModel.from_pretrained(
        model_id,
        subfolder="transformer",
        quantization_config=quant_config,
        torch_dtype=torch_dtype,
    ).to(device)

    text_encoder_2 = T5EncoderModel.from_pretrained(
        model_id,
        subfolder="text_encoder_2",
        quantization_config=quant_config,
        torch_dtype=torch_dtype,
    ).to(device)

    pipe = FluxPipeline.from_pretrained(
        model_id,
        transformer=transformer,
        text_encoder_2=text_encoder_2,
        torch_dtype=torch_dtype,
        token=hf_token,
    )
    pipe = pipe.to(device)
    _PIPELINE_CACHE[cache_key] = pipe
    return pipe, torch_module


def text_to_image(
    prompt: str,
    model_id: str = "black-forest-labs/FLUX.1-schnell",
    width: int = 1024,
    height: int = 1024,
    num_inference_steps: int = 4,
    guidance_scale: float = 0.0,
    seed: int | None = None,
    device: str = "cuda",
    dtype: str = "bfloat16",
    filename: str | None = None,
    return_base64: bool = False,
    max_sequence_length: int = 256,
) -> str:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    width = _validated_dimension(int(width), "width")
    height = _validated_dimension(int(height), "height")

    if int(num_inference_steps) <= 0:
        raise ValueError("num_inference_steps must be greater than 0")
    if int(max_sequence_length) <= 0:
        raise ValueError("max_sequence_length must be greater than 0")

    pipeline, torch_module = _load_pipeline(model_id=model_id, dtype=dtype, device=device)

    generator = None
    if seed is not None:
        generator = torch_module.Generator(device=device).manual_seed(int(seed))

    with torch_module.inference_mode():
        result = pipeline(
            prompt=prompt,
            width=width,
            height=height,
            num_inference_steps=int(num_inference_steps),
            guidance_scale=float(guidance_scale),
            max_sequence_length=int(max_sequence_length),
            generator=generator,
        )

    image = result.images[0]

    output_dir = Path("generated_images")
    output_dir.mkdir(parents=True, exist_ok=True)

    if filename and filename.strip():
        final_name = _safe_filename(filename)
        if not final_name.lower().endswith(".png"):
            final_name += ".png"
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        final_name = f"image_{stamp}.png"

    output_path = output_dir / final_name
    image.save(output_path)
    image_bytes = output_path.read_bytes()

    payload: dict[str, object] = {
        "type": "image",
        "mime_type": "image/png",
        "filename": final_name,
        "path": str(output_path),
        "size_bytes": len(image_bytes),
        "model_id": model_id,
        "width": int(width),
        "height": int(height),
        "seed": seed,
        "num_inference_steps": int(num_inference_steps),
        "guidance_scale": float(guidance_scale),
        "max_sequence_length": int(max_sequence_length),
    }

    if return_base64:
        # Re-encode from PIL output to ensure output is valid PNG in all cases.
        with BytesIO() as buffer:
            image.save(buffer, format="PNG")
            payload["base64"] = base64.b64encode(buffer.getvalue()).decode("ascii")

    return json.dumps(payload, ensure_ascii=True)
