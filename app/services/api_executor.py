import asyncio

import httpx


class ApiExecutor:
    async def call(self, method: str, url: str, headers: dict | None = None, body: dict | None = None) -> dict:
        async with httpx.AsyncClient() as client:
            async with asyncio.timeout(30):
                response = await client.request(method=method.upper(), url=url, headers=headers, json=body)
            return {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": response.text,
            }


api_executor = ApiExecutor()
