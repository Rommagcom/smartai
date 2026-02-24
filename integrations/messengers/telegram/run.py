import asyncio

from integrations.messengers.telegram.adapter import TelegramAdapter


async def main() -> None:
    adapter = TelegramAdapter()
    await adapter.run()


if __name__ == "__main__":
    asyncio.run(main())
