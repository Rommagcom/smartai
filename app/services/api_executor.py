import asyncio

from app.services.egress_policy_service import egress_policy_service
from app.services.http_client_service import http_client_service


class ApiExecutor:
    async def call(self, method: str, url: str, headers: dict | None = None, body: dict | None = None) -> dict:
        safe_url = egress_policy_service.validate_url(url)
        client = http_client_service.get()
        async with asyncio.timeout(30):
            response = await client.request(method=method.upper(), url=safe_url, headers=headers, json=body, timeout=30)
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
        }


api_executor = ApiExecutor()
