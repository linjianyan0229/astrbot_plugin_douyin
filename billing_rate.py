import datetime
import json
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
    group_name: str | None = None
    last_rate_multiplier: float | None = None
    protocol: str = "sub2api"


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


def _build_site_api_url(api_url: str, endpoint_path: str) -> str:
    parts = urlsplit(api_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("计费 API URL 格式无效")

    path = parts.path.rstrip("/")
    known_suffixes = (
        "/v1/sub2api/billing",
        "/v1/chat/completions",
        "/v1/usage",
        "/api/user/self",
        "/api/usage/token",
        "/api/log/token",
        "/api/pricing",
        "/v1",
    )
    for suffix in known_suffixes:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    full_path = f"{path}{endpoint_path}"
    return urlunsplit((parts.scheme, parts.netloc, full_path, parts.query, ""))


def normalize_billing_url(api_url: str) -> str:
    """将站点地址、v1 地址或完整地址统一为 Sub2API billing URL。"""
    return _build_site_api_url(api_url, "/v1/sub2api/billing")


def _http_error_detail(status_code: int) -> str:
    details = {
        401: "API Key 无效或未提供",
        403: "API Key 无权查询",
        404: "站点不支持倍率查询",
        429: "查询请求被限流",
    }
    return details.get(status_code, f"接口返回 HTTP {status_code}")


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _probe_jbb_rate(
    client: httpx.AsyncClient,
    station_name: str,
    api_url: str,
    api_key: str,
    observed_at: str,
) -> BillingRateResult:
    log_url = _build_site_api_url(api_url, "/api/log/token")
    pricing_url = _build_site_api_url(api_url, "/api/pricing")
    log_response = await client.get(
        log_url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if not 200 <= log_response.status_code < 300:
        return BillingRateResult(
            station_name,
            None,
            "failed",
            log_response.status_code,
            _http_error_detail(log_response.status_code),
            observed_at,
            protocol="jbb",
        )

    try:
        log_payload = log_response.json()
        logs = log_payload.get("data", [])
    except (AttributeError, ValueError):
        return BillingRateResult(
            station_name,
            None,
            "failed",
            log_response.status_code,
            "历史记录响应格式异常",
            observed_at,
            protocol="jbb",
        )

    if not isinstance(logs, list) or not logs or not isinstance(logs[0], dict):
        return BillingRateResult(
            station_name,
            None,
            "degraded",
            log_response.status_code,
            "暂无历史记录，无法确定所属分组",
            observed_at,
            protocol="jbb",
        )

    latest_log = logs[0]
    group_name = str(latest_log.get("group") or "").strip()
    other = latest_log.get("other", {})
    if isinstance(other, str):
        try:
            other = json.loads(other)
        except ValueError:
            other = {}
    if not isinstance(other, dict):
        other = {}
    last_rate = _to_float(other.get("group_ratio"))
    if not group_name:
        return BillingRateResult(
            station_name,
            None,
            "degraded",
            log_response.status_code,
            "最近记录中缺少所属分组",
            observed_at,
            last_rate_multiplier=last_rate,
            protocol="jbb",
        )

    pricing_response = await client.get(pricing_url)
    if not 200 <= pricing_response.status_code < 300:
        return BillingRateResult(
            station_name,
            None,
            "degraded",
            pricing_response.status_code,
            "已获取历史分组，但公开定价查询失败",
            observed_at,
            group_name=group_name,
            last_rate_multiplier=last_rate,
            protocol="jbb",
        )

    try:
        pricing_payload = pricing_response.json()
        group_ratios = pricing_payload.get("group_ratio", {})
        if not group_ratios and isinstance(pricing_payload.get("data"), dict):
            group_ratios = pricing_payload["data"].get("group_ratio", {})
        current_rate = _to_float(group_ratios.get(group_name))
    except (AttributeError, ValueError):
        current_rate = None

    state = "healthy" if current_rate is not None else "degraded"
    detail = "公开倍率获取成功" if current_rate is not None else "公开定价中未找到该分组"
    return BillingRateResult(
        station_name,
        current_rate,
        state,
        pricing_response.status_code,
        detail,
        observed_at,
        group_name=group_name,
        last_rate_multiplier=last_rate,
        protocol="jbb",
    )


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
            content_type = response.headers.get("content-type", "").lower()
            should_try_jbb = response.status_code in {404, 405} or (
                response.status_code == 403 and "application/json" not in content_type
            )
            if should_try_jbb:
                return await _probe_jbb_rate(
                    client, station_name, api_url, api_key, observed_at
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
        return BillingRateResult(
            station_name,
            None,
            "failed",
            response.status_code,
            _http_error_detail(response.status_code),
            observed_at,
        )

    try:
        payload = response.json()
        multiplier = float(payload["effective_rate_multiplier"])
    except (KeyError, TypeError, ValueError):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                return await _probe_jbb_rate(
                    client, station_name, api_url, api_key, observed_at
                )
        except httpx.TimeoutException:
            detail = "JBB 倍率查询超时"
        except httpx.RequestError:
            detail = "无法连接到 JBB 倍率接口"
        return BillingRateResult(
            station_name, None, "failed", response.status_code, detail, observed_at
        )

    return BillingRateResult(
        station_name,
        multiplier,
        "healthy",
        response.status_code,
        "倍率获取成功",
        str(payload.get("observed_at") or observed_at),
        group_name=str(payload.get("group_name") or payload.get("group") or "").strip()
        or None,
        protocol="sub2api",
    )


def _format_multiplier(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def render_billing_rates(results: list[BillingRateResult], save_path: str):
    """将多个中转站的实时或历史计费倍率渲染为一张 PNG。"""
    width = 900
    margin = 40
    header_h = 138
    row_h = 132
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
        "yellow": (224, 157, 27),
        "yellow_bg": (255, 244, 215),
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
        "兼容 Sub2API 与 New API/JBB · 不产生 Token 消耗",
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
        state_colors = {
            "healthy": (colors["green"], colors["green_bg"]),
            "degraded": (colors["yellow"], colors["yellow_bg"]),
            "failed": (colors["red"], colors["red_bg"]),
        }
        accent, badge_bg = state_colors.get(
            result.state, (colors["red"], colors["red_bg"])
        )
        draw.rounded_rectangle(
            [margin, row_top, width - margin, row_bottom],
            radius=8,
            fill=colors["surface"],
        )
        draw.rounded_rectangle(
            [margin + 22, row_top + 39, margin + 64, row_top + 81],
            radius=12,
            fill=badge_bg,
        )
        dot = "●"
        dot_w = _text_width(draw, dot, status_font)
        draw.text(
            (margin + 43 - dot_w // 2, row_top + 50),
            dot,
            font=status_font,
            fill=accent,
        )

        station_name = _ellipsize(draw, result.station_name, name_font, 400)
        draw.text(
            (margin + 82, row_top + 17),
            station_name,
            font=name_font,
            fill=colors["text"],
        )
        if result.group_name:
            group_text = f"所属分组：{result.group_name}"
        elif result.protocol == "sub2api":
            group_text = "Sub2API 实时最终倍率"
        else:
            group_text = "所属分组：未知"
        group_text = _ellipsize(draw, group_text, rate_label_font, 410)
        draw.text(
            (margin + 82, row_top + 52),
            group_text,
            font=rate_label_font,
            fill=colors["muted"],
        )
        status_text = result.detail
        status_text = _ellipsize(draw, status_text, detail_font, 410)
        draw.text(
            (margin + 82, row_top + 85),
            status_text,
            font=detail_font,
            fill=colors["muted"] if result.state == "healthy" else accent,
        )

        if result.effective_rate_multiplier is not None:
            rate_text = f"{_format_multiplier(result.effective_rate_multiplier)}x"
            rate_w = _text_width(draw, rate_text, rate_font)
            draw.text(
                (width - margin - 28 - rate_w, row_top + 14),
                rate_text,
                font=rate_font,
                fill=accent,
            )
            label_text = (
                "当前公开倍率" if result.protocol == "jbb" else "实时最终倍率"
            )
            label_w = _text_width(draw, label_text, rate_label_font)
            draw.text(
                (width - margin - 28 - label_w, row_top + 55),
                label_text,
                font=rate_label_font,
                fill=colors["muted"],
            )
        elif result.last_rate_multiplier is not None:
            unknown_text = "--"
            unknown_w = _text_width(draw, unknown_text, rate_font)
            draw.text(
                (width - margin - 28 - unknown_w, row_top + 14),
                unknown_text,
                font=rate_font,
                fill=accent,
            )
            label_text = "当前公开倍率"
            label_w = _text_width(draw, label_text, rate_label_font)
            draw.text(
                (width - margin - 28 - label_w, row_top + 55),
                label_text,
                font=rate_label_font,
                fill=colors["muted"],
            )
        else:
            failed_text = "无法确定" if result.state == "degraded" else "查询失败"
            failed_w = _text_width(draw, failed_text, rate_label_font)
            draw.text(
                (width - margin - 28 - failed_w, row_top + 42),
                failed_text,
                font=rate_label_font,
                fill=accent,
            )

        if result.last_rate_multiplier is not None:
            last_text = (
                f"上次结算 {_format_multiplier(result.last_rate_multiplier)}x"
            )
            last_w = _text_width(draw, last_text, rate_label_font)
            draw.text(
                (width - margin - 28 - last_w, row_top + 88),
                last_text,
                font=rate_label_font,
                fill=colors["muted"],
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
