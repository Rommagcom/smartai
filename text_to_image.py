import torch
from diffusers import FluxPipeline, FluxTransformer2DModel
from transformers import BitsAndBytesConfig, T5EncoderModel

model_id = "black-forest-labs/FLUX.1-schnell"

# 1. Настройка квантования
quant_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_quant_type="nf4"
)

print("Загрузка квантованных компонентов...")
# Загружаем всё сразу на cuda:0 (твоя 5060 Ti)
transformer = FluxTransformer2DModel.from_pretrained(
    model_id, subfolder="transformer", 
    quantization_config=quant_config, torch_dtype=torch.bfloat16
).to("cuda:0")

text_encoder_2 = T5EncoderModel.from_pretrained(
    model_id, subfolder="text_encoder_2", 
    quantization_config=quant_config, torch_dtype=torch.bfloat16
).to("cuda:0")

print("Сборка пайплайна...")
pipe = FluxPipeline.from_pretrained(
    model_id,
    transformer=transformer,
    text_encoder_2=text_encoder_2,
    torch_dtype=torch.bfloat16,
    token="YOUR_HF_TOKEN"
)

# Переносим оставшиеся мелкие части (VAE, CLIP) на ту же карту
pipe.to("cuda:0")

prompt = (
    "A slow, cinematic drone flyover above the ruins of a cyberpunk city. "
    "The girl walks slowly, and touches the robot. "
    "The wind gently moves the clouds. "
    "Golden hour sunlight with realistic lens flares, 4k, highly detailed."
)

print("Генерация на RTX 5060 Ti...")
with torch.inference_mode():
    image = pipe(
        prompt,
        width=1280,
        height=720,
        num_inference_steps=4,
        guidance_scale=0.0,
        max_sequence_length=256,
        generator=torch.Generator("cuda:0").manual_seed(42)
    ).images[0]

image.save("epic_field.png")
print("Готово! Проверь файл epic_field.png")
