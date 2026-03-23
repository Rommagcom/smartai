import torch
from diffusers import WanImageToVideoPipeline, WanTransformer3DModel
from transformers import BitsAndBytesConfig, T5EncoderModel
from diffusers.utils import export_to_video, load_image

model_id = "Wan-AI/Wan2.1-I2V-14B-720P-Diffusers"

# 1. Конфиг для BitsAndBytes
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True
)

print("Загрузка квантованных компонентов (это сэкономит VRAM)...")

# Квантуем основной вычислительный блок (Transformer)
# Он пойдет на твою 5060 Ti
transformer = WanTransformer3DModel.from_pretrained(
    model_id, 
    subfolder="transformer",
    quantization_config=bnb_config,
    torch_dtype=torch.bfloat16
)

# Квантуем текстовый блок (T5)
# Он пойдет на 3060
text_encoder = T5EncoderModel.from_pretrained(
    model_id,
    subfolder="text_encoder",
    quantization_config=bnb_config,
    torch_dtype=torch.bfloat16
)

print("Сборка пайплайна...")
pipe = WanImageToVideoPipeline.from_pretrained(
    model_id,
    transformer=transformer,
    text_encoder=text_encoder,
    torch_dtype=torch.bfloat16,
    device_map="balanced" # Авто-распределение между 5060 Ti и 3060
)

# 2. Подготовка данных для SmartAi
image = load_image("cyber_punk.jpg").resize((1280, 720))
prompt = (
    "A slow, cinematic drone flyover above the ruins of a cyberpunk city. "
    "The girl walks slowly, and touches the robot. "
    "The wind gently moves the clouds. "
    "Golden hour sunlight with realistic lens flares, 4k, highly detailed."
)

print("Запуск генерации видео на архитектуре Blackwell...")
with torch.inference_mode():
    video_frames = pipe(
        image=image,
        prompt=prompt,
        num_frames=81, # ~5 секунд
        num_inference_steps=40,
        guidance_scale=5.0,
    ).frames[0]

output_path = "smartai_wan21_final.mp4"
export_to_video(video_frames, output_path, fps=16)
print(f"Победа! Видео сохранено: {output_path}")