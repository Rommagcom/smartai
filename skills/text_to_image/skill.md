# Text To Image Skill

This dynamic tool generates an image from a text prompt using a Diffusers text-to-image pipeline.

## Tool
- Name: `text_to_image`
- Output: JSON string with image metadata and saved file path.

## Notes
- Default model is `black-forest-labs/FLUX.1-schnell`.
- Image is saved to `generated_images/` as PNG.
- Set `return_base64=true` if the caller needs embedded binary output.

## Usage example

```json
{
  "prompt": "A clean dashboard UI mockup for smart ranch monitoring",
  "width": 1024,
  "height": 1024,
  "seed": 42,
  "return_base64": false
}
```
