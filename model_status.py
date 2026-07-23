import asyncio
import contextlib
import datetime
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image, ImageDraw, ImageFont


_BUNDLED_FONT = Path(__file__).parent / "DouyinSansBold.otf"
_SHANGHAI_TZ = datetime.timezone(datetime.timedelta(hours=8))


@dataclass
class ModelStatusResult:
    cache_key: str
    station_name: str
    model: str
    state: str
    http_status: int | None
    elapsed_ms: float
    ping_ms: float | None
    available: bool
    detail: str
    checked_at: str


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


def normalize_chat_completions_url(api_url: str) -> str:
    """将主机、v1 基础地址或完整地址统一为 chat completions URL。"""
    parts = urlsplit(api_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("模型 API URL 格式无效。")

    path = parts.path.rstrip("/")
    if path.endswith("/chat/completions"):
        endpoint_path = path
    elif path.endswith("/v1"):
        endpoint_path = f"{path}/chat/completions"
    else:
        endpoint_path = f"{path}/v1/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc, endpoint_path, parts.query, ""))


def build_cache_key(
    station_name: str,
    api_url: str,
    model: str,
    api_key: str = "",
) -> str:
    endpoint = normalize_chat_completions_url(api_url)
    source = f"{station_name}\0{endpoint}\0{model}\0{api_key}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()[:20]


def classify_model_status(
    http_status: int | None,
    elapsed_ms: float,
    valid_response: bool,
    degraded_after_ms: float = 10000,
) -> tuple[str, str]:
    if http_status is None:
        return "failed", "连接失败"
    if http_status == 429:
        return "degraded", "接口限流"
    if 200 <= http_status < 300:
        if not valid_response:
            return "degraded", "响应结构异常"
        if elapsed_ms >= degraded_after_ms:
            return "degraded", "响应速度较慢"
        return "healthy", "服务正常"
    return "failed", f"接口返回 HTTP {http_status}"


async def _measure_endpoint_ping(api_url: str) -> float | None:
    parts = urlsplit(api_url.strip())
    origin = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
            await client.head(origin)
    except httpx.HTTPError:
        return None
    return (time.perf_counter() - started) * 1000


async def probe_model_status(
    station_name: str,
    api_url: str,
    api_key: str,
    model: str,
) -> ModelStatusResult:
    """调用一次最小非流式对话请求并测量端到端响应时间。"""
    endpoint = normalize_chat_completions_url(api_url)
    cache_key = build_cache_key(station_name, api_url, model, api_key)
    checked_at = datetime.datetime.now(_SHANGHAI_TZ).isoformat(timespec="seconds")
    ping_task = asyncio.create_task(_measure_endpoint_ping(api_url))
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "请只回复 OK"}],
                    "stream": False,
                },
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
    except asyncio.CancelledError:
        ping_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ping_task
        raise
    except httpx.TimeoutException:
        elapsed_ms = (time.perf_counter() - started) * 1000
        ping_ms = await ping_task
        return ModelStatusResult(
            cache_key,
            station_name,
            model,
            "failed",
            None,
            elapsed_ms,
            ping_ms,
            False,
            "请求超时",
            checked_at,
        )
    except httpx.RequestError:
        elapsed_ms = (time.perf_counter() - started) * 1000
        ping_ms = await ping_task
        return ModelStatusResult(
            cache_key,
            station_name,
            model,
            "failed",
            None,
            elapsed_ms,
            ping_ms,
            False,
            "无法连接到模型接口",
            checked_at,
        )

    ping_ms = await ping_task
    valid_response = False
    if 200 <= response.status_code < 300:
        try:
            payload = response.json()
            valid_response = isinstance(payload, dict) and bool(payload.get("choices"))
        except ValueError:
            pass

    state, detail = classify_model_status(
        response.status_code,
        elapsed_ms,
        valid_response,
    )
    return ModelStatusResult(
        cache_key,
        station_name,
        model,
        state,
        response.status_code,
        elapsed_ms,
        ping_ms,
        200 <= response.status_code < 300 and valid_response,
        detail,
        checked_at,
    )


def _parse_checked_at(value: str) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI_TZ)
    return parsed.astimezone(_SHANGHAI_TZ)


def load_status_history(
    cache_path: str | Path,
    cache_key: str,
    retention_days: int = 7,
) -> list[dict]:
    path = Path(cache_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    records = payload.get("records", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        return []

    cutoff = datetime.datetime.now(_SHANGHAI_TZ) - datetime.timedelta(
        days=retention_days
    )
    history = []
    for record in records:
        if not isinstance(record, dict) or record.get("cache_key") != cache_key:
            continue
        checked_at = _parse_checked_at(record.get("checked_at", ""))
        if checked_at and checked_at >= cutoff:
            history.append(record)
    history.sort(key=lambda item: item.get("checked_at", ""))
    return history


def append_status_history(
    cache_path: str | Path,
    result: ModelStatusResult,
    retention_days: int = 7,
) -> list[dict]:
    path = Path(cache_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {"records": []}

    records = payload.get("records", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        records = []

    cutoff = datetime.datetime.now(_SHANGHAI_TZ) - datetime.timedelta(
        days=retention_days
    )
    retained = []
    for record in records:
        if not isinstance(record, dict):
            continue
        checked_at = _parse_checked_at(record.get("checked_at", ""))
        if checked_at and checked_at >= cutoff:
            retained.append(record)
    retained.append(asdict(result))

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps({"records": retained}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    return [item for item in retained if item.get("cache_key") == result.cache_key]


def result_from_record(record: dict) -> ModelStatusResult:
    return ModelStatusResult(
        cache_key=str(record.get("cache_key", "")),
        station_name=str(record.get("station_name", "")),
        model=str(record.get("model", "")),
        state=str(record.get("state", "failed")),
        http_status=record.get("http_status"),
        elapsed_ms=float(record.get("elapsed_ms", 0)),
        ping_ms=(
            float(record["ping_ms"]) if record.get("ping_ms") is not None else None
        ),
        available=bool(record.get("available", False)),
        detail=str(record.get("detail", "")),
        checked_at=str(record.get("checked_at", "")),
    )


def history_age_seconds(history: list[dict]) -> float | None:
    if not history:
        return None
    checked_at = _parse_checked_at(history[-1].get("checked_at", ""))
    if not checked_at:
        return None
    return max(
        0,
        (datetime.datetime.now(_SHANGHAI_TZ) - checked_at).total_seconds(),
    )


def _build_model_status_image(
    result: ModelStatusResult,
    history: list[dict],
    refresh_interval: int,
) -> Image.Image:
    """将当前状态和缓存历史渲染为监控卡片。"""
    width, height = 760, 720
    colors = {
        "background": (241, 244, 247),
        "surface": (252, 253, 254),
        "panel": (249, 250, 252),
        "text": (24, 31, 45),
        "muted": (148, 158, 177),
        "line": (232, 235, 240),
        "healthy": (20, 190, 126),
        "degraded": (245, 166, 35),
        "failed": (239, 75, 75),
        "empty": (219, 224, 231),
    }
    state_labels = {"healthy": "正常", "degraded": "降级", "failed": "失败"}

    image = Image.new("RGB", (width, height), colors["background"])
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [8, 8, width - 8, height - 8],
        radius=28,
        fill=colors["surface"],
        outline=(207, 213, 221),
        width=2,
    )

    station_font = _get_font(28)
    model_font = _get_font(19)
    tag_font = _get_font(14)
    badge_font = _get_font(17)
    metric_label_font = _get_font(15)
    metric_value_font = _get_font(29)
    metric_unit_font = _get_font(14)
    availability_label_font = _get_font(17)
    availability_font = _get_font(50)
    percent_font = _get_font(24)
    history_font = _get_font(15)
    detail_font = _get_font(13)

    accent = colors.get(result.state, colors["failed"])
    draw.rounded_rectangle([48, 45, 126, 123], radius=20, fill=(226, 250, 240))
    icon_font = _get_font(23)
    icon_text = "AI"
    icon_w = _text_width(draw, icon_text, icon_font)
    draw.text((87 - icon_w // 2, 71), icon_text, font=icon_font, fill=(4, 151, 105))

    station_name = _ellipsize(draw, result.station_name, station_font, 390)
    draw.text((150, 48), station_name, font=station_font, fill=colors["text"])
    draw.rounded_rectangle([150, 91, 207, 121], radius=8, fill=(218, 248, 235))
    draw.text((162, 98), "模型", font=tag_font, fill=(5, 132, 91))
    model_name = _ellipsize(draw, result.model, model_font, 330)
    draw.text((220, 94), model_name, font=model_font, fill=(104, 113, 132))

    status_text = state_labels.get(result.state, "失败")
    http_text = str(result.http_status) if result.http_status is not None else "N/A"
    badge_text = f"{status_text}  {http_text}"
    badge_w = _text_width(draw, badge_text, badge_font) + 34
    badge_box = [width - 48 - badge_w, 47, width - 48, 91]
    if result.state == "healthy":
        badge_bg = (218, 249, 235)
    elif result.state == "degraded":
        badge_bg = (255, 243, 211)
    else:
        badge_bg = (255, 226, 226)
    draw.rounded_rectangle(badge_box, radius=18, fill=badge_bg)
    draw.text(
        (badge_box[0] + 17, badge_box[1] + 10),
        badge_text,
        font=badge_font,
        fill=accent,
    )

    panels = [(48, 165, 362, 325), (382, 165, 712, 325)]
    for panel in panels:
        draw.rounded_rectangle(panel, radius=22, fill=colors["panel"], outline=colors["line"])

    draw.text((78, 197), "⚡  对话延迟", font=metric_label_font, fill=colors["muted"])
    latency_text = f"{result.elapsed_ms:,.0f}"
    draw.text((78, 244), latency_text, font=metric_value_font, fill=colors["text"])
    latency_w = _text_width(draw, latency_text, metric_value_font)
    draw.text((84 + latency_w, 259), "ms", font=metric_unit_font, fill=colors["muted"])

    draw.text((412, 197), "◎  端点 PING", font=metric_label_font, fill=colors["muted"])
    ping_text = f"{result.ping_ms:,.0f}" if result.ping_ms is not None else "N/A"
    draw.text((412, 244), ping_text, font=metric_value_font, fill=colors["text"])
    ping_w = _text_width(draw, ping_text, metric_value_font)
    if result.ping_ms is not None:
        draw.text((418 + ping_w, 259), "ms", font=metric_unit_font, fill=colors["muted"])

    draw.line([(48, 365), (712, 365)], fill=colors["line"], width=1)
    draw.text((48, 414), "可用性 · 7 天", font=availability_label_font, fill=colors["muted"])
    availability = (
        sum(bool(item.get("available")) for item in history) / len(history) * 100
        if history
        else (100 if result.available else 0)
    )
    availability_text = f"{availability:.2f}"
    availability_w = _text_width(draw, availability_text, availability_font)
    value_x = 712 - availability_w - 34
    draw.text((value_x, 392), availability_text, font=availability_font, fill=accent)
    draw.text((value_x + availability_w + 8, 423), "%", font=percent_font, fill=accent)

    draw.line([(48, 505), (712, 505)], fill=colors["line"], width=1)
    draw.text((48, 538), "近 60 次记录", font=history_font, fill=colors["muted"])
    age_seconds = history_age_seconds(history) or 0
    refresh_in = max(0, int(refresh_interval - age_seconds))
    refresh_text = f"{refresh_in}s 后刷新"
    refresh_w = _text_width(draw, refresh_text, history_font)
    draw.text((712 - refresh_w, 538), refresh_text, font=history_font, fill=colors["muted"])

    latest_records = history[-60:]
    slots = [None] * (60 - len(latest_records)) + latest_records
    bar_x = 48
    bar_y = 580
    bar_w = 7
    gap = 4
    for record in slots:
        color = colors["empty"]
        if record:
            color = colors.get(record.get("state"), colors["failed"])
        draw.rounded_rectangle(
            [bar_x, bar_y, bar_x + bar_w, bar_y + 48],
            radius=3,
            fill=color,
        )
        bar_x += bar_w + gap

    draw.text((48, 642), "PAST", font=history_font, fill=colors["muted"])
    now_text = "NOW"
    now_w = _text_width(draw, now_text, history_font)
    draw.text((712 - now_w, 642), now_text, font=history_font, fill=colors["muted"])

    checked_at = _parse_checked_at(result.checked_at)
    checked_text = checked_at.strftime("%Y-%m-%d %H:%M:%S") if checked_at else result.checked_at
    detail_text = f"{result.detail}  ·  更新于 {checked_text}"
    detail_text = _ellipsize(draw, detail_text, detail_font, 664)
    detail_w = _text_width(draw, detail_text, detail_font)
    draw.text(
        ((width - detail_w) // 2, 680),
        detail_text,
        font=detail_font,
        fill=colors["muted"],
    )
    return image


def render_model_status(
    result: ModelStatusResult,
    history: list[dict],
    refresh_interval: int,
    save_path: str,
):
    image = _build_model_status_image(result, history, refresh_interval)
    image.save(save_path, "PNG")


def render_model_status_dashboard(
    cards: list[tuple[ModelStatusResult, list[dict]]],
    refresh_interval: int,
    save_path: str,
):
    """将多个模型状态卡按每行最多三个合并为一张图片。"""
    if not cards:
        raise ValueError("没有可渲染的模型状态卡")
    if len(cards) == 1:
        render_model_status(cards[0][0], cards[0][1], refresh_interval, save_path)
        return

    columns = min(3, len(cards))
    rows = (len(cards) + columns - 1) // columns
    cell_width, cell_height = 600, 568
    gap, padding = 18, 18
    width = padding * 2 + columns * cell_width + (columns - 1) * gap
    height = padding * 2 + rows * cell_height + (rows - 1) * gap
    dashboard = Image.new("RGB", (width, height), (235, 239, 242))
    resampling = getattr(Image, "Resampling", Image).LANCZOS

    for index, (result, history) in enumerate(cards):
        card = _build_model_status_image(result, history, refresh_interval)
        card = card.resize((cell_width, cell_height), resampling)
        row, column = divmod(index, columns)
        x = padding + column * (cell_width + gap)
        y = padding + row * (cell_height + gap)
        dashboard.paste(card, (x, y))

    dashboard.save(save_path, "PNG")
