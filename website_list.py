import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class WebsiteProbeResult:
    url: str
    elapsed_ms: int | None
    error: str | None = None


def _validate_url(url: str) -> str:
    value = str(url or "").strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("网站 URL 格式无效")
    return value


async def probe_website(url: str, timeout: float = 10) -> WebsiteProbeResult:
    """测量从发起 GET 请求到收到响应头的耗时，不下载完整响应正文。"""
    try:
        target_url = _validate_url(url)
    except ValueError as exc:
        return WebsiteProbeResult(str(url or "").strip(), None, str(exc))

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "AstrBot-Website-Monitor/1.0"},
        ) as client:
            async with client.stream("GET", target_url):
                elapsed_ms = max(1, round((time.perf_counter() - started) * 1000))
        return WebsiteProbeResult(target_url, elapsed_ms)
    except httpx.TimeoutException:
        return WebsiteProbeResult(target_url, None, "超时")
    except httpx.RequestError:
        return WebsiteProbeResult(target_url, None, "访问失败")


def format_website_list(results: list[WebsiteProbeResult]) -> str:
    lines = []
    for result in results:
        speed = (
            f"{result.elapsed_ms}ms"
            if result.elapsed_ms is not None
            else (result.error or "访问失败")
        )
        lines.append(f"{result.url}    {speed}")
    return "\n".join(lines)
