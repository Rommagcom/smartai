import torch
from diffusers import WanVideoPipeline
from diffusers.utils import export_to_video

# 1. Настройка модели
# Используем T2V (Text-to-Video) версию 14B в разрешении 720p
model_id = "Wan-AI/Wan2.1-T2V-14B-720P-Diffusers"
output_path = "wan_text_to_video.mp4"
hf_token = "твой_токен_здесь"

print(f"🚀 Загрузка текстовой видео-модели {model_id} в FP8...")

# Загружаем с использованием FP8 весов и автоматическим распределением памяти
pipe = WanVideoPipeline.from_pretrained(
    model_id, 
    torch_dtype=torch.float8_e4m3fn, # Оптимально для 5060 Ti
    token=hf_token,
    device_map="auto",               # Важно для обхода MemoryError
    low_cpu_mem_usage=True
)

# Оптимизации для видеокарты 16GB
pipe.vae.enable_tiling()
pipe.enable_model_cpu_offload()

# 2. Промпт (описание того, что хотим увидеть)
# Чем детальнее описание, тем круче результат Wan2.1
prompt = (
    "A cinematic wide shot of a futuristic neon-lit city in the rain, "
    "flying cars moving between skyscrapers, reflections on the wet pavement, "
    "cyberpunk aesthetic, hyper-realistic, 4k, smooth camera pan."
)
negative_prompt = "blurry, low quality, distorted, static, text, watermark, shaky motion"

# 3. Генерация
print("🎬 Начинаем рендеринг из текста...")
with torch.inference_mode():
    video_frames = pipe(
        prompt=prompt,
        negative_prompt=negative_prompt,
        width=1280,   # 1280 ширина
        height=720,   # 720 высота
        num_frames=81, 
        num_inference_steps=40, 
        guidance_scale=6.0,
    ).frames[0]

# 4. Сохранение
export_to_video(video_frames, output_path, fps=16)
print(f"✨ Видео успешно создано: {output_path}")