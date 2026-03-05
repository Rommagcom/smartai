import asyncio
import os
import tempfile
import uuid
from uuid import UUID

import aiofiles

from app.core.config import settings


class SandboxService:
    async def execute_python_code(self, code: str, user_id: UUID) -> dict:
        script_path = os.path.join(tempfile.gettempdir(), f"assistant_{uuid.uuid4().hex}.py")
        async with aiofiles.open(script_path, mode="w", encoding="utf-8") as file:
            await file.write(code)

        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "run",
                "--rm",
                "--network=none",
                f"--memory={settings.SANDBOX_MEMORY_LIMIT}",
                f"--cpus={settings.SANDBOX_CPU_LIMIT}",
                "--cap-drop=ALL",
                "-v",
                f"{script_path}:/script.py:ro",
                settings.SANDBOX_IMAGE,
                "python",
                "/script.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.SANDBOX_TIMEOUT_SECONDS,
            )
            return {
                "stdout": stdout.decode("utf-8", errors="ignore"),
                "stderr": stderr.decode("utf-8", errors="ignore"),
                "code": process.returncode,
                "success": process.returncode == 0,
                "user_id": str(user_id),
            }
        except TimeoutError:
            return {
                "stdout": "",
                "stderr": f"Execution timeout after {settings.SANDBOX_TIMEOUT_SECONDS} seconds",
                "code": 124,
                "success": False,
                "user_id": str(user_id),
            }
        finally:
            os.unlink(script_path)


sandbox_service = SandboxService()
