import torch
from diffusers import WanImageToVideoPipeline
from diffusers.utils import load_image, export_to_video

# 1. Настройка путей
# Рекомендую версию 720P для 16GB VRAM. 
# Используем официальный diffusers-совместимый репозиторий.
model_id = "Wan-AI/Wan2.1-I2V-14B-720P-Diffusers"
image_path = "smart_ai_v1.png" # Твоя картинка от FLUX
output_path = "wan_result.mp4"

# 2. Загрузка пайплайна
# Для 16ГБ оптимально использовать torch.bfloat16 с включенным CPU offload
# или torch.float8_e4m3fn, если твоя среда поддерживает FP8 (лучше для 5060 Ti)
print("🚀 Загружаем Wan2.1...")
pipe = WanImageToVideoPipeline.from_pretrained(
    model_id, 
    torch_dtype=torch.bfloat16,
    use_safetensors=True,
    token="YOUR_HF_TOKEN",
    low_cpu_mem_usage=True, # Оптимизирует потребление RAM при загрузке
    device_map="balanced"
)

# Оптимизация для 16GB:
# Модель 14B в BF16 весит ~28ГБ, поэтому без offload она не влезет.
# enable_model_cpu_offload() переносит части модели в RAM, когда они не нужны.
pipe.enable_model_cpu_offload()
pipe.vae.enable_tiling() # Важно для рендеринга высокого разрешения

# 3. Подготовка входа
image = load_image(image_path)

# Промпт для видео должен описывать ДВИЖЕНИЕ. 
# Опиши, что именно должно произойти с объектом на картинке FLUX.
prompt = "The character in the image slowly turns their head and smiles, cinematic lighting, soft bokeh, high quality"
negative_prompt = "static, blurry, low quality, distorted, jittery motion"

# 4. Генерация
print("🎬 Генерация видео началась...")
# num_frames: для Wan2.1 формула (4*k + 1). 81 кадр = ~5 сек при 16fps.
video_frames = pipe(
    prompt=prompt,
    image=image,
    negative_prompt=negative_prompt,
    num_frames=81,
    num_inference_steps=40,
    guidance_scale=5.0,
).frames[0]

# 5. Сохранение
export_to_video(video_frames, output_path, fps=16)
print(f"✅ Готово! Видео сохранено в {output_path}")
