import datetime
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image, ImageDraw, ImageFont


_BUNDLED_FONT = Path(__file__).parent / "DouyinSansBold.otf"
_SHANGHAI_TZ = datetime.timezone(datetime.timedelta(hours=8))


@dataclass
class BillingRateResult:
    station_name: str
    effective_rate_multiplier: float | None
    state: str
    http_status: int | None
    detail: str
    observed_at: str


def _get_font(size: int):
    try:
        return ImageFont.truetype(str(_BUNDLED_FONT), size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _text_width(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _ellipsize(draw, text: str, font, max_width: int) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    suffix = "..."
    while text and _text_width(draw, text + suffix, font) > max_width:
        text = text[:-1]
    return text + suffix


def normalize_billing_url(api_url: str) -> str:
    """将站点地址、v1 地址或完整地址统一为 billing URL。"""
    parts = urlsplit(api_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("计费 API URL 格式无效")

    path = parts.path.rstrip("/")
    if path.endswith("/v1/sub2api/billing"):
        endpoint_path = path
    elif path.endswith("/v1/chat/completions"):
        endpoint_path = f"{path[:-len('/chat/completions')]}/sub2api/billing"
    elif path.endswith("/v1"):
        endpoint_path = f"{path}/sub2api/billing"
    else:
        endpoint_path = f"{path}/v1/sub2api/billing"
    return urlunsplit((parts.scheme, parts.netloc, endpoint_path, parts.query, ""))


async def probe_billing_rate(
    station_name: str,
    api_url: str,
    api_key: str,
) -> BillingRateResult:
    """使用普通模型 API Key 查询该 Key 当前生效的计费倍率。"""
    observed_at = datetime.datetime.now(_SHANGHAI_TZ).isoformat(timespec="seconds")
    try:
        endpoint = normalize_billing_url(api_url)
    except ValueError as exc:
        return BillingRateResult(
            station_name, None, "failed", None, str(exc), observed_at
        )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.TimeoutException:
        return BillingRateResult(
            station_name, None, "failed", None, "请求超时", observed_at
        )
    except httpx.RequestError:
        return BillingRateResult(
            station_name, None, "failed", None, "无法连接到计费接口", observed_at
        )

    if not 200 <= response.status_code < 300:
        details = {
            401: "API Key 无效或未提供",
            403: "API Key 无权查询",
            404: "站点不支持倍率查询",
            429: "查询请求被限流",
        }
        return BillingRateResult(
            station_name,
            None,
            "failed",
            response.status_code,
            details.get(response.status_code, f"接口返回 HTTP {response.status_code}"),
            observed_at,
        )

    try:
        payload = response.json()
        multiplier = float(payload["effective_rate_multiplier"])
    except (KeyError, TypeError, ValueError):
        return BillingRateResult(
            station_name,
            None,
            "failed",
            response.status_code,
            "响应中缺少最终计费倍率",
            observed_at,
        )

    return BillingRateResult(
        station_name,
        multiplier,
        "healthy",
        response.status_code,
        "倍率获取成功",
        str(payload.get("observed_at") or observed_at),
    )


def _format_multiplier(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def render_billing_rates(results: list[BillingRateResult], save_path: str):
    """将多个中转站的最终计费倍率渲染为一张 PNG。"""
    width = 900
    margin = 40
    header_h = 138
    row_h = 104
    footer_h = 58
    height = margin * 2 + header_h + max(len(results), 1) * row_h + footer_h
    colors = {
        "background": (242, 245, 247),
        "surface": (255, 255, 255),
        "text": (28, 36, 44),
        "muted": (126, 137, 148),
        "line": (230, 234, 237),
        "green": (19, 164, 112),
        "green_bg": (224, 248, 239),
        "red": (216, 70, 70),
        "red_bg": (255, 232, 232),
    }

    image = Image.new("RGB", (width, height), colors["background"])
    draw = ImageDraw.Draw(image)
    title_font = _get_font(32)
    subtitle_font = _get_font(15)
    name_font = _get_font(21)
    status_font = _get_font(13)
    rate_font = _get_font(31)
    rate_label_font = _get_font(15)
    detail_font = _get_font(13)

    draw.text((margin, margin + 8), "中转站分组倍率", font=title_font, fill=colors["text"])
    draw.text(
        (margin, margin + 60),
        "普通 API Key 实时查询 · 不产生 Token 消耗",
        font=subtitle_font,
        fill=colors["muted"],
    )
    draw.rounded_rectangle(
        [margin, margin + 96, margin + 80, margin + 101],
        radius=2,
        fill=colors["green"],
    )

    content_top = margin + header_h
    if not results:
        results = [
            BillingRateResult(
                "暂无中转站", None, "failed", None, "尚未配置查询组", ""
            )
        ]

    for index, result in enumerate(results):
        row_top = content_top + index * row_h
        row_bottom = row_top + row_h - 10
        healthy = result.state == "healthy"
        accent = colors["green"] if healthy else colors["red"]
        badge_bg = colors["green_bg"] if healthy else colors["red_bg"]
        draw.rounded_rectangle(
            [margin, row_top, width - margin, row_bottom],
            radius=8,
            fill=colors["surface"],
        )
        draw.rounded_rectangle(
            [margin + 22, row_top + 25, margin + 64, row_top + 67],
            radius=12,
            fill=badge_bg,
        )
        dot = "●"
        dot_w = _text_width(draw, dot, status_font)
        draw.text(
            (margin + 43 - dot_w // 2, row_top + 36),
            dot,
            font=status_font,
            fill=accent,
        )

        station_name = _ellipsize(draw, result.station_name, name_font, 420)
        draw.text(
            (margin + 82, row_top + 22),
            station_name,
            font=name_font,
            fill=colors["text"],
        )
        status_text = (
            f"HTTP {result.http_status} · {result.detail}"
            if result.http_status is not None
            else result.detail
        )
        status_text = _ellipsize(draw, status_text, detail_font, 430)
        draw.text(
            (margin + 82, row_top + 57),
            status_text,
            font=detail_font,
            fill=colors["muted"] if healthy else accent,
        )

        if result.effective_rate_multiplier is not None:
            rate_text = f"{_format_multiplier(result.effective_rate_multiplier)}x"
            rate_w = _text_width(draw, rate_text, rate_font)
            draw.text(
                (width - margin - 28 - rate_w, row_top + 17),
                rate_text,
                font=rate_font,
                fill=accent,
            )
            label_text = "分组倍率"
            label_w = _text_width(draw, label_text, rate_label_font)
            draw.text(
                (width - margin - 28 - label_w, row_top + 59),
                label_text,
                font=rate_label_font,
                fill=colors["muted"],
            )
        else:
            failed_text = "查询失败"
            failed_w = _text_width(draw, failed_text, rate_label_font)
            draw.text(
                (width - margin - 28 - failed_w, row_top + 39),
                failed_text,
                font=rate_label_font,
                fill=accent,
            )

    checked_at = datetime.datetime.now(_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    footer_text = f"查询时间  {checked_at}  |  Asia/Shanghai"
    footer_w = _text_width(draw, footer_text, detail_font)
    draw.text(
        ((width - footer_w) // 2, height - margin - 24),
        footer_text,
        font=detail_font,
        fill=colors["muted"],
    )
    image.save(save_path, "PNG")
