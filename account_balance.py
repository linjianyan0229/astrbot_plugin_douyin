from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image, ImageDraw, ImageFont


QUOTA_PER_USD = 500_000
_BUNDLED_FONT = Path(__file__).parent / "DouyinSansBold.otf"


@dataclass(frozen=True)
class AccountBalanceResult:
    name: str
    success: bool
    balance_usd: float | None = None
    used_usd: float | None = None
    request_count: int | None = None
    unlimited: bool = False
    detail: str = ""
    mode: str | None = None
    today_used_usd: float | None = None
    request_period: str = ""
    protocol: str = "new_api"


def _normalize_endpoint(api_url: str, endpoint_path: str) -> str:
    parts = urlsplit(str(api_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("中转站 URL 格式无效")

    path = parts.path.rstrip("/")
    for suffix in ("/api/user/self", "/v1/usage", "/v1"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, f"{path}{endpoint_path}", "", ""))


def normalize_user_self_url(api_url: str) -> str:
    return _normalize_endpoint(api_url, "/api/user/self")


def normalize_usage_url(api_url: str) -> str:
    return _normalize_endpoint(api_url, "/v1/usage")


def _http_error_detail(status_code: int) -> str:
    details = {
        401: "访问令牌或 API Key 无效或已过期",
        403: "API Key、用户 ID 或访问权限无效",
        404: "站点不支持余额查询接口",
        429: "查询请求被限流",
    }
    return details.get(status_code, f"接口返回 HTTP {status_code}")


async def _request_balance_json(
    client: httpx.AsyncClient,
    name: str,
    endpoint: str,
    headers: dict[str, str],
) -> tuple[dict | None, AccountBalanceResult | None]:
    try:
        response = await client.get(endpoint, headers=headers)
    except httpx.TimeoutException:
        return None, AccountBalanceResult(name, False, detail="请求超时")
    except httpx.RequestError:
        return None, AccountBalanceResult(name, False, detail="无法连接到余额接口")

    if not 200 <= response.status_code < 300:
        return None, AccountBalanceResult(
            name, False, detail=_http_error_detail(response.status_code)
        )

    try:
        payload = response.json()
    except ValueError:
        return None, AccountBalanceResult(
            name, False, detail="余额接口响应不是有效 JSON"
        )
    if not isinstance(payload, dict):
        return None, AccountBalanceResult(name, False, detail="余额接口响应格式异常")
    return payload, None


def _parse_new_api_balance(name: str, payload: dict) -> AccountBalanceResult:
    if not payload.get("success"):
        detail = str(payload.get("message") or "余额查询失败")
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


def _parse_sub2api_balance(name: str, payload: dict) -> AccountBalanceResult:
    try:
        usage = payload["usage"]
        today = usage["today"]
        total = usage["total"]
        remaining = float(payload["remaining"])
        today_used = float(today["actual_cost"])
        total_used = float(total["actual_cost"])
        today_requests = int(today["requests"])
    except (KeyError, TypeError, ValueError):
        return AccountBalanceResult(
            name,
            False,
            detail="Sub2API 余额数据字段不完整",
            protocol="sub2api",
        )

    return AccountBalanceResult(
        name=name,
        success=True,
        balance_usd=remaining,
        used_usd=total_used,
        request_count=today_requests,
        unlimited=remaining < 0,
        mode=str(payload.get("mode") or "").strip() or None,
        today_used_usd=today_used,
        request_period="今日",
        protocol="sub2api",
    )


async def probe_account_balance(
    name: str,
    api_url: str,
    api_key: str,
    user_id: str = "",
    client: httpx.AsyncClient | None = None,
) -> AccountBalanceResult:
    try:
        new_api_endpoint = normalize_user_self_url(api_url)
        usage_endpoint = normalize_usage_url(api_url)
    except ValueError as exc:
        return AccountBalanceResult(name, False, detail=str(exc))

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True)
    new_api_error = None
    try:
        if user_id:
            payload, new_api_error = await _request_balance_json(
                client,
                name,
                new_api_endpoint,
                {
                    "Authorization": f"Bearer {api_key}",
                    "New-Api-User": str(user_id),
                },
            )
            if payload is not None:
                new_api_result = _parse_new_api_balance(name, payload)
                if new_api_result.success:
                    return new_api_result
                new_api_error = new_api_result

        payload, sub2api_error = await _request_balance_json(
            client,
            name,
            usage_endpoint,
            {"Authorization": f"Bearer {api_key}"},
        )
        if payload is not None:
            sub2api_result = _parse_sub2api_balance(name, payload)
            if sub2api_result.success:
                return sub2api_result
            sub2api_error = sub2api_result
        elif sub2api_error is not None:
            sub2api_error = AccountBalanceResult(
                name,
                False,
                detail=sub2api_error.detail,
                protocol="sub2api",
            )
        return new_api_error or sub2api_error or AccountBalanceResult(
            name, False, detail="余额查询失败"
        )
    finally:
        if owns_client:
            await client.aclose()


def _get_font(size: int):
    try:
        return ImageFont.truetype(str(_BUNDLED_FONT), size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    suffix = "..."
    while text and _text_width(draw, text + suffix, font) > max_width:
        text = text[:-1]
    return text + suffix


def render_account_balances(
    results: list[AccountBalanceResult],
    save_path: str | Path,
):
    if not results:
        raise ValueError("没有可渲染的余额数据")

    width = 1100
    margin = 40
    title_h = 122
    header_h = 58
    row_h = 76
    footer_h = 52
    table_top = title_h
    body_top = table_top + header_h
    height = body_top + len(results) * row_h + footer_h

    bg = (244, 246, 248)
    panel = (255, 255, 255)
    header = (31, 35, 43)
    ink = (31, 35, 42)
    muted = (112, 119, 132)
    line = (222, 226, 232)
    green = (23, 145, 103)
    orange = (215, 126, 35)
    blue = (43, 113, 246)
    red = (210, 67, 78)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    title_font = _get_font(36)
    subtitle_font = _get_font(16)
    header_font = _get_font(17)
    name_font = _get_font(20)
    body_font = _get_font(19)
    detail_font = _get_font(13)
    footer_font = _get_font(14)

    draw.text((margin, 34), "中转站余额", font=title_font, fill=ink)
    draw.text((margin, 84), "账号资产概览", font=subtitle_font, fill=muted)
    draw.text(
        (width - margin, 64),
        f"共 {len(results)} 个账号",
        font=subtitle_font,
        fill=muted,
        anchor="rm",
    )

    x0 = margin
    x1 = 370
    x2 = 650
    x3 = 910
    x4 = width - margin
    draw.rounded_rectangle(
        (x0, table_top, x4, height - footer_h + 2),
        radius=8,
        fill=panel,
    )
    draw.rounded_rectangle(
        (x0, table_top, x4, body_top),
        radius=8,
        fill=header,
    )
    draw.rectangle((x0, body_top - 8, x4, body_top), fill=header)

    headers = (
        ("中转站名称", x0 + 24, "lm"),
        ("余额", x1 + 24, "lm"),
        ("累计消耗", x2 + 24, "lm"),
        ("次数", x4 - 24, "rm"),
    )
    for label, x, anchor in headers:
        draw.text(
            (x, table_top + header_h // 2),
            label,
            font=header_font,
            fill=(247, 248, 251),
            anchor=anchor,
        )

    for index, result in enumerate(results):
        top = body_top + index * row_h
        center_y = top + row_h // 2
        if index:
            draw.line((x0 + 20, top, x4 - 20, top), fill=line, width=1)
        if index % 2:
            draw.rectangle((x0, top, x4, top + row_h), fill=(250, 251, 253))

        name = _ellipsize(draw, result.name, name_font, x1 - x0 - 52)
        if result.success:
            draw.rectangle((x0, top, x0 + 6, top + row_h), fill=green)
            name_y = center_y - 9 if result.mode else center_y
            draw.text((x0 + 24, name_y), name, font=name_font, fill=ink, anchor="lm")
            if result.mode:
                mode = _ellipsize(
                    draw,
                    f"模式: {result.mode}",
                    detail_font,
                    x1 - x0 - 52,
                )
                draw.text(
                    (x0 + 24, center_y + 16),
                    mode,
                    font=detail_font,
                    fill=muted,
                    anchor="lm",
                )
            balance = (
                "无限额度"
                if result.unlimited
                else f"${result.balance_usd:.{8 if result.protocol == 'sub2api' else 6}f}"
            )
            draw.text(
                (x1 + 24, center_y),
                balance,
                font=body_font,
                fill=green,
                anchor="lm",
            )
            used_y = center_y - 9 if result.today_used_usd is not None else center_y
            draw.text(
                (x2 + 24, used_y),
                f"${result.used_usd:.{8 if result.protocol == 'sub2api' else 6}f}",
                font=body_font,
                fill=orange,
                anchor="lm",
            )
            if result.today_used_usd is not None:
                draw.text(
                    (x2 + 24, center_y + 16),
                    f"今日 ${result.today_used_usd:.8f}",
                    font=detail_font,
                    fill=muted,
                    anchor="lm",
                )
            count_y = center_y - 9 if result.request_period else center_y
            draw.text(
                (x4 - 24, count_y),
                str(result.request_count),
                font=body_font,
                fill=blue,
                anchor="rm",
            )
            if result.request_period:
                draw.text(
                    (x4 - 24, center_y + 16),
                    result.request_period,
                    font=detail_font,
                    fill=muted,
                    anchor="rm",
                )
        else:
            draw.rectangle((x0, top, x0 + 6, top + row_h), fill=red)
            draw.text((x0 + 24, center_y - 9), name, font=name_font, fill=ink, anchor="lm")
            detail = _ellipsize(draw, result.detail, detail_font, x1 - x0 - 52)
            draw.text(
                (x0 + 24, center_y + 16),
                detail,
                font=detail_font,
                fill=muted,
                anchor="lm",
            )
            draw.text(
                (x1 + 24, center_y),
                "查询失败",
                font=body_font,
                fill=red,
                anchor="lm",
            )
            draw.text((x2 + 24, center_y), "-", font=body_font, fill=muted, anchor="lm")
            draw.text((x4 - 24, center_y), "-", font=body_font, fill=muted, anchor="rm")

    draw.text(
        (margin, height - footer_h // 2),
        "金额单位 USD",
        font=footer_font,
        fill=muted,
        anchor="lm",
    )
    draw.text(
        (width - margin, height - footer_h // 2),
        "实时查询",
        font=footer_font,
        fill=muted,
        anchor="rm",
    )
    image.save(save_path, "PNG", optimize=True)
