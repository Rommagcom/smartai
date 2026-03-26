# Text To Video Skill

This dynamic tool generates an MP4 video from a text prompt using a Diffusers text-to-video pipeline.

## Tool
- Name: text_to_video
- Output: JSON string with video metadata and saved file path.

## Notes
- Default model is Wan-AI/Wan2.1-T2V-14B-Diffusers.
- Output video is saved to generated_videos/ as MP4.
- GPU is strongly recommended for acceptable generation time.

## Usage example

```json
{
  "prompt": "A cinematic drone shot flying above a rainy cyberpunk city at night",
  "negative_prompt": "blurry, low quality, watermark",
  "width": 1280,
  "height": 720,
  "num_frames": 81,
  "num_inference_steps": 40,
  "guidance_scale": 6.0,
  "fps": 16,
  "seed": 42
}
```
