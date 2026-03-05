from __future__ import annotations

import asyncio

from app.workers.worker_service import worker_service


async def main() -> None:
    await worker_service.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
