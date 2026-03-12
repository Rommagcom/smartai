# Weather Custom Skill

This is a minimal Dynamic Skill template.

Input params example:
- city: string

Returns:
- ok: boolean
- city: string
- forecast: string

Contract:
- Function name: run
- Signature: run(params, context)

Optional capability:
- Uses context.llm.chat(...) when enabled in manifest.
