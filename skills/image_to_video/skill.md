# Image To Video Skill

This dynamic tool generates an MP4 video from a source image and text prompt using a Diffusers image-to-video pipeline.

## Tool
- Name: image_to_video
- Output: JSON string with video metadata and saved file path.

## Notes
- Default model is Wan-AI/Wan2.1-I2V-14B-Diffusers.
- Input image can be a local path or URL.
- Output video is saved to generated_videos/ as MP4.
- GPU is strongly recommended for acceptable generation time.

## Usage example

```json
{
  "image": "generated_images/hero.png",
  "prompt": "The character slowly turns toward camera, wind in the hair, cinematic lighting",
  "negative_prompt": "blurry, low quality, jittery motion, watermark",
  "width": 1280,
  "height": 720,
  "num_frames": 81,
  "num_inference_steps": 40,
  "guidance_scale": 5.0,
  "fps": 16,
  "seed": 123
}
```
