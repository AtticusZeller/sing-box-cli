"""
Sing-Box API Client module.
Provides an async client for interacting with Sing-Box HTTP API.
"""

import json
from collections.abc import AsyncGenerator
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field


class FrozenBaseModel(BaseModel):
    """Base model with frozen configuration."""

    model_config = ConfigDict(frozen=True)


# Define Pydantic models for API responses
class TrafficData(FrozenBaseModel):
    """http://base_url/traffic/"""

    up: int = Field(default=0, description="Upload speed in B/s")
    down: int = Field(default=0, description="Download speed in B/s")


class MemoryData(FrozenBaseModel):
    """http://base_url/memory/"""

    inuse: int = Field(default=0, description="Memory in use in bytes")
    total: int = Field(default=0, description="Total memory in bytes")


class ConnectionMetadata(FrozenBaseModel):
    destinationIP: str = ""
    destinationPort: str
    dnsMode: str
    host: str = ""
    network: str = Field(description="Network type")
    processPath: str = ""
    sourceIP: str
    sourcePort: str
    type: str = Field(description="Inbound type")


class ConnectionInfo(FrozenBaseModel):
    """https://base_url/connections/"""

    id: str
    download: int = Field(default=0, description="Download in bytes")
    upload: int = Field(default=0, description="Upload in bytes")
    rule: str = Field(description="Rule name => outbound")
    start: str = Field(description="Start time")
    chains: list[str] = Field(description="Proxy chains list")
    metadata: ConnectionMetadata


class ConnectionData(FrozenBaseModel):
    uploadTotal: int = Field(default=0, description="Total upload in bytes")
    downloadTotal: int = Field(default=0, description="Total download in bytes")
    memory: int = Field(default=0, description="Memory usage in bytes")
    connections: list[ConnectionInfo] = Field(description="List of active connections")


class DelayHistory(FrozenBaseModel):
    time: str = ""
    delay: int = Field(default=0, description="Outbound delay in milliseconds")


class GroupInfo(FrozenBaseModel):
    """http://base_url/group/{group_name}/"""

    type: str = Field(description="Proxy Group type")
    name: str
    udp: bool = Field(description="UDP support")
    history: list[DelayHistory] = Field(
        description="History delay of selected proxies", default=[]
    )
    now: str = Field(description="Currently selected proxy")
    all: list[str] = Field(description="List of all outbound proxies")


class GroupsData(FrozenBaseModel):
    """http://base_url/group/"""

    proxies: list[GroupInfo] = Field(description="List of proxies in the group")


class DelayTestResult(FrozenBaseModel):
    """Delay test result model."""

    outbound: str = Field(default="", description="Outbound proxy name")
    delay: int = Field(default=0, description="Outbound delay in milliseconds")


class LogEntry(FrozenBaseModel):
    type: str = Field(description="Logs level")
    payload: str = Field(description="Log message")


# Generic response type for API methods
T = TypeVar("T", bound=FrozenBaseModel)


# TODO： exception handling for sing-box stop working or not reachable, SpinnerColumn for retry or connecting status
class SingBoxAPIClient:
    """Async client for interacting with Sing-Box API.

    Args:
        base_url: The base URL of the Sing-Box API
        token: The API authentication token
        timeout: Request timeout in seconds
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:9090",
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout = timeout
        if not self._health_check():
            raise ValueError(f"Invalid to initialize client: {self.base_url}")

    def _health_check(self) -> bool:
        """Check if the API is reachable.

        Returns:
            True if the API is reachable, False otherwise
        """
        try:
            response = httpx.get(
                f"{self.base_url}", timeout=self.timeout, headers=self.headers
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def _make_request_raw(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Make a request to the API.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint
            params: URL parameters
            data: Request body data

        Returns:
            JSON response as dictionary

        Raises:
            httpx.HTTPError: If the request fails
        """
        url = f"{self.base_url}{endpoint}"
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=url,
                params=params,
                json=data,
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            if response.content:
                return dict(response.json())
            return {}

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        model_class: type[T],
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> T:
        """Make a request and return data as a specific model."""
        raw = await self._make_request_raw(method, endpoint, params, data)
        return model_class.model_validate(raw)

    async def _make_stream_request(
        self, endpoint: str, model_class: type[T], params: dict[str, str] | None = None
    ) -> AsyncGenerator[T, None]:
        """
        Make a streaming request to the API.

        Args:
            endpoint: API endpoint
            model_class: Pydantic model class to validate response data
            params: Optional query parameters for the request

        Yields:
            Validated model instances from the stream
        """
        url = f"{self.base_url}{endpoint}"

        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET", url, headers=self.headers, params=params, timeout=None
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.strip():  # Skip empty lines
                        try:
                            data = json.loads(line)
                            yield model_class.model_validate(data)
                        except json.JSONDecodeError:
                            # Skip invalid JSON lines
                            continue

    async def traffic_stream(self) -> AsyncGenerator[TrafficData, None]:
        """
        Get traffic statistics as a stream of updates.

        Yields:
            TrafficData object containing traffic data (up/down in B/s)
        """
        async for data in self._make_stream_request("/traffic", TrafficData):
            yield data

    async def memory_stream(self) -> AsyncGenerator[MemoryData, None]:
        """
        Get memory statistics as a stream of updates.

        Yields:
            MemoryData object containing memory data (inuse/total in bytes)
        """
        async for data in self._make_stream_request("/memory", MemoryData):
            yield data

    async def log_stream(
        self, level: str | None = None
    ) -> AsyncGenerator[LogEntry, None]:
        """
        Get log entries as a stream of updates.

        Yields:
            LogEntry object containing log data (type/payload)
        """
        params = {"level": level} if level else None
        async for data in self._make_stream_request("/logs", LogEntry, params=params):
            yield data

    async def get_connections(self) -> ConnectionData:
        """
        Get current connections.

        Returns:
            ConnectionData object containing active connections
        """
        return await self._make_request(
            "GET", "/connections", model_class=ConnectionData
        )

    async def close_connection(self, connection_id: str) -> dict[str, Any]:
        """
        Close a specific connection.

        Args:
            connection_id: ID of the connection to close

        Returns:
            Response from the API
        """
        return await self._make_request_raw("DELETE", f"/connections/{connection_id}")

    async def close_all_connections(self) -> dict[str, Any]:
        """
        Close all connections.

        Returns:
            Response from the API
        """
        return await self._make_request_raw("DELETE", "/connections")

    async def get_groups(self) -> GroupsData:
        """
        Get all policy groups.

        Returns:
            GroupData object containing policy groups information
        """
        return await self._make_request("GET", "/group", model_class=GroupsData)

    async def get_group(self, group_name: str) -> GroupInfo:
        """
        Get information about a specific policy group.

        Args:
            group_name: Name of the policy group

        Returns:
            GroupData object containing the policy group information
        """
        return await self._make_request(
            "GET", f"/group/{group_name}", model_class=GroupInfo
        )

    async def test_group_delay(
        self,
        group_name: str,
        url: str = "https://cp.cloudflare.com/generate_204",
        timeout: int = 5000,
    ) -> list[DelayTestResult]:
        """
        Test delay for all proxies in a policy group.

        Args:
            group_name: Name of the policy group
            timeout: Timeout in milliseconds
        """
        params = {"url": url, "timeout": timeout}
        reps = await self._make_request_raw(
            "GET", f"/group/{group_name}/delay", params=params
        )
        return [
            DelayTestResult(outbound=key, delay=value) for key, value in reps.items()
        ]

    async def test_proxy_delay(
        self,
        proxy_name: str,
        url: str = "https://cp.cloudflare.com/generate_204",
        timeout: int = 5000,
    ) -> DelayTestResult:
        """
        Test delay for a specific proxy.

        Args:
            timeout: Timeout in milliseconds
        """
        params = {"url": url, "timeout": timeout}
        return await self._make_request(
            "GET",
            f"/proxies/{proxy_name}/delay",
            params=params,
            model_class=DelayTestResult,
        )

    async def select_proxy(self, proxy_name: str, selected: str) -> dict[str, Any]:
        """
        Select a proxy for a selector proxy group.

        Args:
            proxy_name: Name of the proxy selector
            selected: Name of the proxy to select
        """
        data = {"name": selected}
        return await self._make_request_raw("PUT", f"/proxies/{proxy_name}", data=data)

    async def get_version(self) -> dict[str, Any]:
        """
        Get Sing-Box version.

        Returns:
            Dictionary containing version information
        """
        return await self._make_request_raw("GET", "/version")
