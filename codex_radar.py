import datetime
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont


CODEX_RADAR_URL = "https://codexradar.com/data/intelligence-efficiency.json"
CACHE_TTL_SECONDS = 600
_BUNDLED_FONT = Path(__file__).parent / "DouyinSansBold.otf"


class CodexRadarError(Exception):
    pass


@dataclass(frozen=True)
class CodexRadarSnapshot:
    data: dict
    from_cache: bool
    stale: bool


def _get_font(size: int):
    try:
        return ImageFont.truetype(str(_BUNDLED_FONT), size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _validate_payload(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("响应不是 JSON 对象")
    points = payload.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("响应中缺少模型数据")
    required = {"model", "effort", "iq", "average_price_usd", "average_minutes"}
    if not all(isinstance(item, dict) and required <= item.keys() for item in points):
        raise ValueError("模型数据字段不完整")
    return payload


def _load_cache(cache_path: Path) -> tuple[dict | None, float | None]:
    try:
        payload = _validate_payload(
            json.loads(cache_path.read_text(encoding="utf-8"))
        )
        return payload, cache_path.stat().st_mtime
    except (OSError, ValueError, TypeError):
        return None, None


def _save_cache(cache_path: Path, payload: dict):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(f"{cache_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temp_path.replace(cache_path)


async def fetch_codex_radar(
    cache_path: str | Path,
    cache_ttl: int = CACHE_TTL_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> CodexRadarSnapshot:
    """读取 Codex Radar 数据；新鲜缓存优先，请求失败时回退旧缓存。"""
    path = Path(cache_path)
    cached, cached_at = _load_cache(path)
    if cached is not None and cached_at is not None:
        if time.time() - cached_at < max(0, cache_ttl):
            return CodexRadarSnapshot(cached, from_cache=True, stale=False)

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=15, follow_redirects=True)
    try:
        response = await client.get(
            CODEX_RADAR_URL,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = _validate_payload(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        if cached is not None:
            return CodexRadarSnapshot(cached, from_cache=True, stale=True)
        raise CodexRadarError(f"数据获取失败: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    try:
        _save_cache(path, payload)
    except OSError:
        pass
    return CodexRadarSnapshot(payload, from_cache=False, stale=False)


def _format_updated_at(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "未知"
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(draw: ImageDraw.ImageDraw, xy, value, font, fill, anchor=None):
    draw.text(xy, str(value), font=font, fill=fill, anchor=anchor)


def render_codex_radar(
    payload: dict,
    save_path: str | Path,
    stale: bool = False,
):
    data = _validate_payload(payload)
    points = data["points"]

    width = 1200
    margin = 48
    title_h = 150
    table_head_h = 58
    row_h = 62
    footer_h = 72
    table_top = title_h
    body_top = table_top + table_head_h
    height = body_top + len(points) * row_h + footer_h

    bg = (244, 246, 248)
    ink = (28, 31, 38)
    muted = (112, 119, 132)
    line = (221, 225, 231)
    panel = (255, 255, 255)
    header = (30, 34, 42)
    accent = (43, 113, 246)
    iq_track = (229, 235, 245)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    title_font = _get_font(38)
    subtitle_font = _get_font(17)
    header_font = _get_font(17)
    model_font = _get_font(19)
    body_font = _get_font(17)
    metric_font = _get_font(18)
    footer_font = _get_font(15)

    _text(draw, (margin, 38), "Codex 雷达", title_font, ink)
    _text(draw, (margin, 92), "智力效率排行", subtitle_font, muted)
    updated = _format_updated_at(data.get("source_updated_at"))
    status = "旧缓存" if stale else "10 分钟缓存"
    _text(
        draw,
        (width - margin, 72),
        f"更新 {updated}  |  {len(points)} 个档位  |  {status}",
        subtitle_font,
        muted if not stale else (194, 117, 20),
        anchor="rm",
    )

    x0 = margin
    x1 = 390
    x2 = 585
    x3 = 815
    x4 = 1010
    x5 = width - margin

    draw.rounded_rectangle(
        (x0, table_top, x5, height - footer_h + 2),
        radius=8,
        fill=panel,
    )
    draw.rounded_rectangle(
        (x0, table_top, x5, body_top),
        radius=8,
        fill=header,
    )
    draw.rectangle((x0, body_top - 8, x5, body_top), fill=header)

    headers = (
        ("模型", x0 + 24, "lm"),
        ("推理强度", x1 + 20, "lm"),
        ("智力 IQ", x2 + 20, "lm"),
        ("平均价格", x4 - 20, "rm"),
        ("平均耗时", x5 - 24, "rm"),
    )
    for label, x, anchor in headers:
        _text(draw, (x, table_top + table_head_h // 2), label, header_font, (246, 248, 252), anchor)

    model_colors = {
        "gpt-5.6-sol": (43, 113, 246),
        "gpt-5.6-terra": (22, 150, 115),
        "gpt-5.6-luna": (140, 83, 207),
        "gpt-5.5": (230, 120, 40),
    }
    effort_colors = {
        "low": (86, 132, 203),
        "medium": (42, 145, 120),
        "high": (225, 139, 43),
        "xhigh": (217, 91, 101),
        "max": (146, 83, 195),
        "ultra": (66, 76, 94),
    }
    previous_model = None
    max_iq = max((_float(item.get("iq")) or 0 for item in points), default=1)
    max_iq = max(max_iq, 1)

    for index, item in enumerate(points):
        top = body_top + index * row_h
        center_y = top + row_h // 2
        model = str(item.get("model") or "-")
        effort = str(item.get("effort") or "-")
        if model != previous_model:
            if index:
                draw.line((x0, top, x5, top), fill=(188, 195, 205), width=2)
            draw.rectangle(
                (x0, top, x0 + 6, top + row_h),
                fill=model_colors.get(model, accent),
            )
        else:
            draw.line((x0 + 20, top, x5 - 20, top), fill=line, width=1)

        if index % 2:
            draw.rectangle((x0 + 6, top, x5, top + row_h), fill=(250, 251, 253))

        _text(draw, (x0 + 24, center_y), model, model_font, ink, "lm")

        effort_color = effort_colors.get(effort, muted)
        badge_left = x1 + 20
        badge_right = badge_left + 102
        draw.rounded_rectangle(
            (badge_left, center_y - 16, badge_right, center_y + 16),
            radius=6,
            fill=tuple(min(255, int(channel + (255 - channel) * 0.84)) for channel in effort_color),
        )
        _text(draw, ((badge_left + badge_right) // 2, center_y), effort, body_font, effort_color, "mm")

        iq = _float(item.get("iq"))
        iq_text = f"{iq:.1f}" if iq is not None else "-"
        bar_left = x2 + 86
        bar_right = x3 - 24
        draw.rounded_rectangle(
            (bar_left, center_y - 5, bar_right, center_y + 5),
            radius=5,
            fill=iq_track,
        )
        if iq is not None:
            fill_right = bar_left + int((bar_right - bar_left) * max(0, iq) / max_iq)
            draw.rounded_rectangle(
                (bar_left, center_y - 5, max(bar_left + 5, fill_right), center_y + 5),
                radius=5,
                fill=accent,
            )
        _text(draw, (x2 + 20, center_y), iq_text, metric_font, ink, "lm")

        price = _float(item.get("average_price_usd"))
        minutes = _float(item.get("average_minutes"))
        price_text = f"${price:.1f}" if price is not None else "-"
        minutes_text = f"{minutes:.0f} min" if minutes is not None else "-"
        _text(draw, (x4 - 20, center_y), price_text, metric_font, ink, "rm")
        _text(draw, (x5 - 24, center_y), minutes_text, metric_font, ink, "rm")
        previous_model = model

    footer_y = height - footer_h // 2
    _text(draw, (margin, footer_y), "数据来源 codexradar.com", footer_font, muted, "lm")
    _text(draw, (width - margin, footer_y), "IQ = 任务通过率 × 150", footer_font, muted, "rm")
    image.save(save_path, "PNG", optimize=True)
