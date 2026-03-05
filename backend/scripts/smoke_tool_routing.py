import asyncio


async def run() -> None:
    await asyncio.sleep(0)
    print("SMOKE_TOOL_ROUTING_SKIPPED (web tool routing removed)")


if __name__ == "__main__":
    asyncio.run(run())
