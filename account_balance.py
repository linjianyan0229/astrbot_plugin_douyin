from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx


QUOTA_PER_USD = 500_000


@dataclass(frozen=True)
class AccountBalanceResult:
    name: str
    success: bool
    balance_usd: float | None = None
    used_usd: float | None = None
    request_count: int | None = None
    unlimited: bool = False
    detail: str = ""


def normalize_user_self_url(api_url: str) -> str:
    parts = urlsplit(str(api_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("中转站 URL 格式无效")

    path = parts.path.rstrip("/")
    for suffix in ("/api/user/self", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, f"{path}/api/user/self", "", ""))


def _http_error_detail(status_code: int) -> str:
    details = {
        401: "访问令牌无效或已过期",
        403: "用户 ID 或访问权限无效",
        404: "站点不支持余额查询接口",
        429: "查询请求被限流",
    }
    return details.get(status_code, f"接口返回 HTTP {status_code}")


async def probe_account_balance(
    name: str,
    api_url: str,
    api_key: str,
    user_id: str,
    client: httpx.AsyncClient | None = None,
) -> AccountBalanceResult:
    try:
        endpoint = normalize_user_self_url(api_url)
    except ValueError as exc:
        return AccountBalanceResult(name, False, detail=str(exc))

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True)
    try:
        response = await client.get(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "New-Api-User": str(user_id),
            },
        )
    except httpx.TimeoutException:
        return AccountBalanceResult(name, False, detail="请求超时")
    except httpx.RequestError:
        return AccountBalanceResult(name, False, detail="无法连接到余额接口")
    finally:
        if owns_client:
            await client.aclose()

    if not 200 <= response.status_code < 300:
        return AccountBalanceResult(
            name,
            False,
            detail=_http_error_detail(response.status_code),
        )

    try:
        payload = response.json()
    except ValueError:
        return AccountBalanceResult(name, False, detail="余额接口响应不是有效 JSON")

    if not isinstance(payload, dict) or not payload.get("success"):
        detail = "余额查询失败"
        if isinstance(payload, dict):
            detail = str(payload.get("message") or detail)
        return AccountBalanceResult(name, False, detail=detail)

    try:
        user = payload["data"]
        quota = float(user["quota"])
        used_quota = float(user["used_quota"])
        request_count = int(user.get("request_count", 0))
    except (KeyError, TypeError, ValueError):
        return AccountBalanceResult(name, False, detail="余额数据字段不完整")

    return AccountBalanceResult(
        name=name,
        success=True,
        balance_usd=quota / QUOTA_PER_USD,
        used_usd=used_quota / QUOTA_PER_USD,
        request_count=request_count,
        unlimited=quota < 0,
    )


def format_account_balances(results: list[AccountBalanceResult]) -> str:
    lines = []
    for result in results:
        if not result.success:
            lines.append(f"{result.name}: 查询失败，{result.detail}")
            continue
        balance = "无限额度" if result.unlimited else f"${result.balance_usd:.6f}"
        lines.append(
            f"{result.name}: 余额 {balance}，"
            f"累计消耗 ${result.used_usd:.6f}，"
            f"请求数 {result.request_count}"
        )
    return "\n".join(lines)
