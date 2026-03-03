import asyncio


async def run() -> None:
    await asyncio.sleep(0)
    print("SMOKE_WORKER_QUEUE_SKIPPED (web worker jobs removed)")


if __name__ == "__main__":
    asyncio.run(run())
