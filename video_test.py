import torch
from diffusers import StableVideoDiffusionPipeline
from diffusers.utils import load_image, export_to_video

# 1. Загрузка модели
model_id = "stabilityai/stable-video-diffusion-img2vid-xt" # XT версия для 25 кадров

print("Загрузка SVD на RTX 5060 Ti...")
pipe = StableVideoDiffusionPipeline.from_pretrained(
    model_id, 
    torch_dtype=torch.float16, # SVD лучше работает в float16
    variant="fp16"
)

# Используем твою 5060 Ti (GPU 0)
pipe.to("cuda:0")

# Включаем микро-оптимизацию памяти для видео
pipe.enable_model_cpu_offload()

# 2. Подготовка изображения
# Возьмем изображение, которое мы сгенерировали ранее (например, со стадом)
image_path = "cyber_punk.jpg" 
image = load_image(image_path)
image = image.resize((1024, 1024)) # SVD лучше всего работает с этим разрешением

print(f"Генерация анимации для {image_path}...")

# 3. Запуск генерации видео
# motion_bucket_id: чем выше число (до 255), тем больше движения в кадре
# noise_aug_strength: уровень изменения картинки (0.02 - стандарт)
with torch.inference_mode():
    frames = pipe(
        image, 
        decode_chunk_size=8, 
        motion_bucket_id=127, 
        fps=7,
        noise_aug_strength=0.02,
        generator=torch.Generator("cuda:0").manual_seed(42)
    ).frames[0]

# 4. Сохранение результата
video_path = "smartai_drone_shot.mp4"
export_to_video(frames, video_path, fps=22)

print(f"---")
print(f"Видео готово! Файл сохранен как {video_path}")