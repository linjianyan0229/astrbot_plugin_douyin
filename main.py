from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
import httpx
import re
import datetime
from pypinyin import lazy_pinyin
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path


DOUYIN_URL_PATTERN = re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_\-]+/?")
PARSE_API = "https://toody.netlify.app/.netlify/functions/parse"
CMA_API_BASE = "https://weather.cma.cn/api"

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------- Pillow weather card helpers ----------

_BUNDLED_FONT = Path(__file__).parent / "DouyinSansBold.otf"


def _get_font(size):
    try:
        return ImageFont.truetype(str(_BUNDLED_FONT), size)
    except (OSError, IOError):
        return ImageFont.load_default()


def _draw_vgradient(draw, bbox, c1, c2):
    """Fill rectangle with a vertical linear gradient"""
    x1, y1, x2, y2 = bbox
    h = y2 - y1
    for i in range(h):
        r = c1[0] + (c2[0] - c1[0]) * i // max(h - 1, 1)
        g = c1[1] + (c2[1] - c1[1]) * i // max(h - 1, 1)
        b = c1[2] + (c2[2] - c1[2]) * i // max(h - 1, 1)
        draw.line([(x1, y1 + i), (x2, y1 + i)], fill=(r, g, b))


def _center_text(draw, text, y, canvas_w, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((canvas_w - tw) // 2, y), text, font=font, fill=fill)


def _text_w(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


# ---------- Card renderers ----------


def _render_weather_now(data, save_path):
    """Draw current-weather card with Pillow and save as PNG"""
    W, CX, CW, R = 400, 20, 360, 24
    BG = (240, 244, 248)
    WHITE = (255, 255, 255)
    GRAD_H = 190
    DX, DW = CX + 16, CW - 32
    ROW_H = 46
    DPAD = 20
    DH = DPAD + 4 * ROW_H + DPAD - 6

    alarms = data.get("alarms", [])
    alarm_h = (14 + 20 + len(alarms) * 20 + 14) if alarms else 0

    detail_y = GRAD_H
    alarm_y = detail_y + DH + 12
    footer_y = (alarm_y + alarm_h if alarms else detail_y + DH) + 16
    card_h = footer_y + 20 + 24
    H = 20 + card_h + 20
    card_y = 20

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # gradient top with rounded upper corners
    grad = Image.new("RGB", (CW, GRAD_H))
    gd = ImageDraw.Draw(grad)
    _draw_vgradient(gd, (0, 0, CW, GRAD_H), (79, 172, 254), (30, 144, 255))
    mask = Image.new("L", (CW, GRAD_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, CW, GRAD_H + R], radius=R, fill=255)
    img.paste(grad, (CX, card_y), mask)

    # header text
    f14 = _get_font(14)
    f64 = _get_font(64)
    dim = (230, 235, 245)
    _center_text(draw, data["path"], card_y + 45, W, f14, dim)
    _center_text(draw, f"{data['temperature']}°", card_y + 70, W, f64, WHITE)
    _center_text(draw, f"体感 {data['feelst']}°C", card_y + 148, W, f14, dim)

    # detail card
    dy = card_y + detail_y
    draw.rounded_rectangle([DX, dy, DX + DW, dy + DH], radius=16, fill=WHITE)

    rows = [
        ("湿度", f"{data['humidity']}%"),
        (
            "风况",
            f"{data['wind_direction']} {data['wind_speed']}m/s {data['wind_scale']}",
        ),
        ("降水", f"{data['precipitation']}mm"),
        ("气压", f"{data['pressure']}hPa"),
    ]
    f15 = _get_font(15)
    ry = dy + DPAD
    for i, (label, value) in enumerate(rows):
        draw.text((DX + 24, ry + 13), label, font=f14, fill=(153, 153, 153))
        vw = _text_w(draw, value, f15)
        draw.text((DX + DW - 24 - vw, ry + 12), value, font=f15, fill=(51, 51, 51))
        if i < len(rows) - 1:
            draw.line(
                [(DX + 16, ry + ROW_H - 1), (DX + DW - 16, ry + ROW_H - 1)],
                fill=(240, 240, 245),
                width=1,
            )
        ry += ROW_H

    # alarm section
    if alarms:
        ay = card_y + alarm_y
        draw.rounded_rectangle(
            [DX, ay, DX + DW, ay + alarm_h], radius=12, fill=(255, 243, 224)
        )
        f13 = _get_font(13)
        draw.text((DX + 18, ay + 14), "预警信息", font=f13, fill=(230, 81, 0))
        f12 = _get_font(12)
        for j, alarm in enumerate(alarms):
            txt = alarm if isinstance(alarm, str) else str(alarm.get("title", alarm))
            draw.text((DX + 18, ay + 38 + j * 20), txt, font=f12, fill=(191, 54, 12))

    # footer
    f11 = _get_font(11)
    _center_text(
        draw,
        f"更新: {data['last_update']} | 数据来源: 中国气象局",
        card_y + footer_y,
        W,
        f11,
        (187, 187, 187),
    )

    img.save(save_path, "PNG")


def _render_weather_forecast(data, save_path):
    """Draw 7-day forecast card with Pillow and save as PNG"""
    W, CX, CW, R = 400, 20, 360, 24
    BG = (240, 244, 248)
    WHITE = (255, 255, 255)
    GRAD_H = 130
    DX, DW = CX + 16, CW - 32
    CARD_H, GAP = 80, 10

    daily = data["daily"]
    daily_h = len(daily) * CARD_H + max(len(daily) - 1, 0) * GAP

    daily_y = GRAD_H
    footer_y = daily_y + daily_h + 16
    total_h = footer_y + 20 + 24
    H = 20 + total_h + 20
    card_y = 20

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # gradient top
    grad = Image.new("RGB", (CW, GRAD_H))
    gd = ImageDraw.Draw(grad)
    _draw_vgradient(gd, (0, 0, CW, GRAD_H), (102, 126, 234), (118, 75, 162))
    mask = Image.new("L", (CW, GRAD_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, CW, GRAD_H + R], radius=R, fill=255)
    img.paste(grad, (CX, card_y), mask)

    # header
    f14 = _get_font(14)
    f22 = _get_font(22)
    dim = (230, 235, 245)
    _center_text(draw, data["path"], card_y + 38, W, f14, dim)
    _center_text(draw, "未来7天天气预报", card_y + 62, W, f22, WHITE)

    # daily cards
    f15 = _get_font(15)
    f12 = _get_font(12)
    f20 = _get_font(20)
    f13 = _get_font(13)

    dy = card_y + daily_y
    for i, day in enumerate(daily):
        cy = dy + i * (CARD_H + GAP)
        draw.rounded_rectangle([DX, cy, DX + DW, cy + CARD_H], radius=14, fill=WHITE)

        # date + weekday
        draw.text((DX + 18, cy + 16), day["date_short"], font=f15, fill=(51, 51, 51))
        dw = _text_w(draw, day["date_short"], f15)
        draw.text(
            (DX + 18 + dw + 8, cy + 18),
            day["weekday"],
            font=f12,
            fill=(153, 153, 153),
        )

        # high / low temps (right-aligned)
        parts = [
            (f"{day['high']}°", (231, 76, 60)),
            (" / ", (204, 204, 204)),
            (f"{day['low']}°", (52, 152, 219)),
        ]
        tw = sum(_text_w(draw, t, f20) for t, _ in parts)
        tx = DX + DW - 18 - tw
        for text, color in parts:
            draw.text((tx, cy + 14), text, font=f20, fill=color)
            tx += _text_w(draw, text, f20)

        # day / night details
        day_detail = (
            f"白天 {day['day_text']} {day['day_wind_dir']} {day['day_wind_scale']}"
        )
        night_detail = (
            f"夜间 {day['night_text']} {day['night_wind_dir']} "
            f"{day['night_wind_scale']}"
        )
        draw.text((DX + 18, cy + 50), day_detail, font=f13, fill=(102, 102, 102))
        nw = _text_w(draw, night_detail, f13)
        draw.text(
            (DX + DW - 18 - nw, cy + 50),
            night_detail,
            font=f13,
            fill=(102, 102, 102),
        )

    # footer
    f11 = _get_font(11)
    _center_text(
        draw,
        f"更新: {data['last_update']} | 数据来源: 中国气象局",
        card_y + footer_y,
        W,
        f11,
        (187, 187, 187),
    )

    img.save(save_path, "PNG")


@register(
    "astrbot_plugin_douyin",
    "linjianyan0229",
    "自动检测抖音分享链接，解析并发送无水印视频",
    "1.0.0",
)
class DouyinPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            self._data_dir = (
                Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_douyin"
            )
        except ImportError:
            self._data_dir = Path("data") / "plugin_data" / "astrbot_plugin_douyin"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def _check_whitelist(self, event: AstrMessageEvent, prefix: str = ""):
        """检查白名单权限。返回 None 表示放行，返回 "BLOCKED" 表示静默拦截，
        返回 MessageEventResult 表示拦截并回复。"""
        if self.config.get(f"{prefix}enable_id_whitelist", False):
            id_whitelist = self.config.get(f"{prefix}id_whitelist", [])
            umo = event.unified_msg_origin
            if id_whitelist and umo not in id_whitelist:
                reply = self.config.get(f"{prefix}whitelist_reply", "")
                if reply:
                    return event.plain_result(reply)
                return "BLOCKED"

        group_id = event.message_obj.group_id
        if group_id:
            whitelist = self.config.get(f"{prefix}group_whitelist", [])
            if whitelist and str(group_id) not in [str(g) for g in whitelist]:
                return "BLOCKED"
        else:
            if not self.config.get(f"{prefix}enable_in_private", True):
                return "BLOCKED"

        return None

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1)
    async def on_message(self, event: AstrMessageEvent):
        denied = self._check_whitelist(event, "")
        if denied is not None:
            if denied != "BLOCKED":
                yield denied
            return

        text = event.message_str
        match = DOUYIN_URL_PATTERN.search(text)
        if not match:
            return

        douyin_url = match.group(0)
        logger.info(f"检测到抖音链接: {douyin_url}")

        # 调用解析 API
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            try:
                resp = await client.get(PARSE_API, params={"url": douyin_url})
                data = resp.json()
            except Exception as e:
                logger.error(f"抖音解析 API 请求失败: {e}")
                yield event.plain_result(f"抖音解析失败: {e}")
                return

        if data.get("code") != 1:
            logger.warning(f"抖音解析失败: {data.get('msg', '未知错误')}")
            yield event.plain_result(f"抖音解析失败: {data.get('msg', '未知错误')}")
            return

        content_type = data.get("type", "")
        title = data.get("title", "")
        author = data.get("name", "")
        video_url = data.get("video", "")

        if content_type == "视频" and video_url:
            info_text = f"作者: {author}\n{title}" if title else f"作者: {author}"
            yield event.plain_result(info_text)
            yield event.chain_result([Comp.Video.fromURL(video_url)])
        elif content_type == "图集" and data.get("images"):
            info_text = f"作者: {author}\n{title}" if title else f"作者: {author}"
            yield event.plain_result(info_text)
            for img_url in data["images"]:
                yield event.chain_result([Comp.Image.fromURL(img_url)])
        else:
            yield event.plain_result(
                f"抖音解析成功但暂不支持该内容类型: {content_type}"
            )

    def _make_weather_client(self):
        """创建带浏览器请求头的 HTTP 客户端"""
        return httpx.AsyncClient(
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Referer": "https://weather.cma.cn/",
            },
        )

    async def _search_station(self, client: httpx.AsyncClient, city: str):
        """搜索城市获取气象站信息，返回 (station_id, station_name) 或 None"""
        pinyin_query = "".join(lazy_pinyin(city))
        resp = await client.get(
            f"{CMA_API_BASE}/autocomplete", params={"q": pinyin_query}
        )
        data = resp.json()
        if data.get("code") != 0 or not data.get("data"):
            return None
        for item in data["data"]:
            parts = item.split("|")
            if parts[1] == city:
                return parts[0], parts[1]
        first = data["data"][0]
        parts = first.split("|")
        return parts[0], parts[1]

    @filter.command("天气")
    async def weather_help(self, event: AstrMessageEvent):
        """天气查询帮助"""
        denied = self._check_whitelist(event, "weather_")
        if denied is not None:
            if denied != "BLOCKED":
                yield denied
            return

        help_text = (
            "【天气查询指令】\n"
            "1. 查询天气 <城市> - 查询城市实时天气\n"
            "   示例: 查询天气 北京\n"
            "2. 查询未来天气 <城市> - 查询城市未来7天天气预报\n"
            "   示例: 查询未来天气 上海\n"
            "3. 天气 - 显示本帮助信息\n"
            "\n数据来源: 中国气象局"
        )
        yield event.plain_result(help_text)

    @filter.command("查询天气")
    async def query_weather_now(self, event: AstrMessageEvent, city: str):
        """查询指定城市的实时天气"""
        denied = self._check_whitelist(event, "weather_")
        if denied is not None:
            if denied != "BLOCKED":
                yield denied
            return

        async with self._make_weather_client() as client:
            try:
                result = await self._search_station(client, city)
                if not result:
                    yield event.plain_result(f"未找到城市: {city}")
                    return
                station_id, station_name = result

                resp = await client.get(f"{CMA_API_BASE}/now/{station_id}")
                weather_data = resp.json()
            except Exception as e:
                logger.error(f"天气查询失败: {e}")
                yield event.plain_result(f"天气查询失败: {e}")
                return

        if weather_data.get("code") != 0:
            yield event.plain_result("获取天气数据失败")
            return

        d = weather_data["data"]
        now = d["now"]

        card_data = {
            "path": d["location"]["path"],
            "temperature": now["temperature"],
            "feelst": now["feelst"],
            "humidity": now["humidity"],
            "wind_direction": now["windDirection"],
            "wind_speed": now["windSpeed"],
            "wind_scale": now["windScale"],
            "precipitation": now["precipitation"],
            "pressure": now["pressure"],
            "alarms": d.get("alarm", []),
            "last_update": d.get("lastUpdate", "未知"),
        }

        img_path = str(self._data_dir / "weather_now.png")
        _render_weather_now(card_data, img_path)
        yield event.image_result(img_path)

    @filter.command("查询未来天气")
    async def query_weather_forecast(self, event: AstrMessageEvent, city: str):
        """查询指定城市未来7天天气预报"""
        denied = self._check_whitelist(event, "weather_")
        if denied is not None:
            if denied != "BLOCKED":
                yield denied
            return

        async with self._make_weather_client() as client:
            try:
                result = await self._search_station(client, city)
                if not result:
                    yield event.plain_result(f"未找到城市: {city}")
                    return
                station_id, station_name = result

                resp = await client.get(f"{CMA_API_BASE}/weather/{station_id}")
                forecast_data = resp.json()
            except Exception as e:
                logger.error(f"天气预报查询失败: {e}")
                yield event.plain_result(f"天气预报查询失败: {e}")
                return

        if forecast_data.get("code") != 0:
            yield event.plain_result("获取天气预报数据失败")
            return

        d = forecast_data["data"]
        daily = d.get("daily", [])

        daily_items = []
        for day in daily:
            dt = datetime.datetime.strptime(day["date"], "%Y/%m/%d")
            daily_items.append(
                {
                    "date_short": f"{dt.month}/{dt.day}",
                    "weekday": WEEKDAY_NAMES[dt.weekday()],
                    "high": f"{day['high']:.0f}",
                    "low": f"{day['low']:.0f}",
                    "day_text": day["dayText"],
                    "day_wind_dir": day["dayWindDirection"],
                    "day_wind_scale": day["dayWindScale"],
                    "night_text": day["nightText"],
                    "night_wind_dir": day["nightWindDirection"],
                    "night_wind_scale": day["nightWindScale"],
                }
            )

        card_data = {
            "path": d["location"]["path"],
            "daily": daily_items,
            "last_update": d.get("lastUpdate", "未知"),
        }

        img_path = str(self._data_dir / "weather_forecast.png")
        _render_weather_forecast(card_data, img_path)
        yield event.image_result(img_path)

    async def terminate(self):
        pass
