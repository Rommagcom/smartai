import asyncio


async def run() -> None:
    await asyncio.sleep(0)
    print("SMOKE_CHAT_SELF_SERVICE_SKIPPED (web tools removed)")


if __name__ == "__main__":
    asyncio.run(run())
