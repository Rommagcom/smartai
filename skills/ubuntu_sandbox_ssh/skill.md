# Ubuntu Sandbox SSH Skill

This tool connects to the Ubuntu sandbox container over SSH and executes shell commands.

## Purpose
- Run arbitrary Linux commands in isolated sandbox container.
- Inspect files, processes, networking, and package state.
- Perform admin operations through `sudo` when needed.

## Defaults
- Host: `ubuntu-sandbox`
- Port: `22`
- Username: `sandbox`
- Password: `sandbox`

The tool also reads optional environment defaults:
- `SANDBOX_SSH_HOST`
- `SANDBOX_SSH_PORT`
- `SANDBOX_SSH_USER`
- `SANDBOX_SSH_PASSWORD`

## Example

```json
{
  "command": "whoami; id; uname -a",
  "timeout_seconds": 60
}
```
