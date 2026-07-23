import datetime
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont


_BUNDLED_FONT = Path(__file__).parent / "DouyinSansBold.otf"


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


def _format_tokens(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value or 0)


def _format_amount(value) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value or 0)


class RankingAPIError(Exception):
    """消费排行 API 请求或返回数据异常。"""


async def fetch_users_ranking(api_url: str, api_key: str, limit: int = 12):
    """查询上海时区本周周一至今天的消费排行。"""
    shanghai_tz = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(shanghai_tz).date()
    week_start = today - datetime.timedelta(days=today.weekday())
    params = {
        "start_date": week_start.isoformat(),
        "end_date": today.isoformat(),
        "timezone": "Asia/Shanghai",
        "limit": limit,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                api_url,
                headers={"x-api-key": api_key},
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise RankingAPIError(
            f"接口返回 HTTP {exc.response.status_code}。"
        ) from exc
    except httpx.HTTPError as exc:
        raise RankingAPIError("请检查接口地址和服务状态。") from exc
    except ValueError as exc:
        raise RankingAPIError("接口未返回有效的 JSON 数据。") from exc

    if not isinstance(payload, dict):
        raise RankingAPIError("接口返回数据格式异常。")
    if payload.get("code") != 0:
        raise RankingAPIError(str(payload.get("message") or "接口返回失败。"))

    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("ranking", []), list):
        raise RankingAPIError("接口返回的排行格式异常。")

    return data


def render_users_ranking(data: dict, save_path: str):
    """将消费排行渲染为 PNG 图片。"""
    ranking = [item for item in data.get("ranking", []) if isinstance(item, dict)]

    width = 1000
    margin = 48
    title_h = 138
    table_header_h = 52
    row_h = 64
    empty_h = 96
    summary_h = 96
    footer_h = 38
    body_h = len(ranking) * row_h if ranking else empty_h
    height = margin * 2 + title_h + table_header_h + body_h + summary_h + footer_h

    colors = {
        "background": (243, 246, 248),
        "surface": (255, 255, 255),
        "title": (27, 35, 43),
        "muted": (112, 122, 132),
        "line": (226, 231, 235),
        "header": (35, 47, 58),
        "green": (20, 132, 99),
        "gold": (218, 164, 38),
        "silver": (126, 139, 151),
        "bronze": (174, 104, 61),
    }
    image = Image.new("RGB", (width, height), colors["background"])
    draw = ImageDraw.Draw(image)

    title_font = _get_font(34)
    subtitle_font = _get_font(16)
    header_font = _get_font(16)
    row_font = _get_font(18)
    rank_font = _get_font(17)
    summary_label_font = _get_font(15)
    summary_value_font = _get_font(25)
    footer_font = _get_font(13)

    card_left = margin
    card_right = width - margin
    table_top = margin + title_h
    draw.rounded_rectangle(
        [card_left, table_top, card_right, height - margin - footer_h],
        radius=8,
        fill=colors["surface"],
    )

    draw.text(
        (margin, margin + 8),
        "全球最稳中转站排行榜",
        font=title_font,
        fill=colors["title"],
    )
    date_text = (
        f"统计周期  {data.get('start_date', '未知')} 至 "
        f"{data.get('end_date', '未知')}  |  Asia/Shanghai"
    )
    draw.text(
        (margin, margin + 62),
        date_text,
        font=subtitle_font,
        fill=colors["muted"],
    )
    draw.rounded_rectangle(
        [margin, margin + 100, margin + 86, margin + 105],
        radius=2,
        fill=colors["green"],
    )

    draw.rectangle(
        [card_left, table_top, card_right, table_top + table_header_h],
        fill=colors["header"],
    )
    rank_x = margin + 44
    user_x = margin + 105
    token_right = margin + 705
    amount_right = card_right - 34
    header_y = table_top + 16
    draw.text((rank_x - 18, header_y), "排名", font=header_font, fill=(235, 239, 242))
    draw.text((user_x, header_y), "用户名", font=header_font, fill=(235, 239, 242))
    token_label = "TOKEN"
    draw.text(
        (token_right - _text_width(draw, token_label, header_font), header_y),
        token_label,
        font=header_font,
        fill=(235, 239, 242),
    )
    amount_label = "金额"
    draw.text(
        (amount_right - _text_width(draw, amount_label, header_font), header_y),
        amount_label,
        font=header_font,
        fill=(235, 239, 242),
    )

    body_top = table_top + table_header_h
    if ranking:
        medal_colors = [colors["gold"], colors["silver"], colors["bronze"]]
        for index, item in enumerate(ranking, 1):
            row_top = body_top + (index - 1) * row_h
            if index % 2 == 0:
                draw.rectangle(
                    [card_left, row_top, card_right, row_top + row_h],
                    fill=(249, 250, 251),
                )
            draw.line(
                [(card_left + 20, row_top + row_h), (card_right - 20, row_top + row_h)],
                fill=colors["line"],
            )

            center_y = row_top + row_h // 2
            if index <= 3:
                draw.ellipse(
                    [rank_x - 17, center_y - 17, rank_x + 17, center_y + 17],
                    fill=medal_colors[index - 1],
                )
                rank_text = str(index)
                rank_w = _text_width(draw, rank_text, rank_font)
                draw.text(
                    (rank_x - rank_w // 2, center_y - 11),
                    rank_text,
                    font=rank_font,
                    fill=(255, 255, 255),
                )
            else:
                rank_text = str(index)
                rank_w = _text_width(draw, rank_text, rank_font)
                draw.text(
                    (rank_x - rank_w // 2, center_y - 11),
                    rank_text,
                    font=rank_font,
                    fill=colors["muted"],
                )

            username = str(
                item.get("username")
                or item.get("email")
                or f"用户 {item.get('user_id', '未知')}"
            )
            username = _ellipsize(draw, username, row_font, 390)
            draw.text(
                (user_x, center_y - 12),
                username,
                font=row_font,
                fill=colors["title"],
            )

            token_text = _format_tokens(item.get("tokens", 0))
            draw.text(
                (token_right - _text_width(draw, token_text, row_font), center_y - 12),
                token_text,
                font=row_font,
                fill=colors["title"],
            )
            amount_text = _format_amount(item.get("actual_cost", 0))
            draw.text(
                (amount_right - _text_width(draw, amount_text, row_font), center_y - 12),
                amount_text,
                font=row_font,
                fill=colors["green"],
            )
    else:
        empty_text = "当前统计周期暂无消费记录"
        empty_w = _text_width(draw, empty_text, row_font)
        draw.text(
            ((width - empty_w) // 2, body_top + 34),
            empty_text,
            font=row_font,
            fill=colors["muted"],
        )

    summary_top = body_top + body_h
    draw.rectangle(
        [card_left, summary_top, card_right, summary_top + summary_h],
        fill=(237, 247, 243),
    )
    draw.text(
        (margin + 32, summary_top + 19),
        "全部用户总计",
        font=summary_label_font,
        fill=colors["muted"],
    )
    draw.text(
        (margin + 32, summary_top + 45),
        "统计周期内所有用户",
        font=footer_font,
        fill=colors["muted"],
    )

    total_tokens = _format_tokens(data.get("total_tokens", 0))
    draw.text(
        (margin + 370, summary_top + 16),
        "TOKEN",
        font=summary_label_font,
        fill=colors["muted"],
    )
    draw.text(
        (margin + 370, summary_top + 42),
        total_tokens,
        font=summary_value_font,
        fill=colors["title"],
    )
    total_amount = _format_amount(data.get("total_actual_cost", 0))
    amount_width = _text_width(draw, total_amount, summary_value_font)
    draw.text(
        (amount_right - _text_width(draw, "金额", summary_label_font), summary_top + 16),
        "金额",
        font=summary_label_font,
        fill=colors["muted"],
    )
    draw.text(
        (amount_right - amount_width, summary_top + 42),
        total_amount,
        font=summary_value_font,
        fill=colors["green"],
    )

    footer_text = "按实际消费金额降序，其次按 Token 降序"
    footer_width = _text_width(draw, footer_text, footer_font)
    draw.text(
        ((width - footer_width) // 2, height - margin - 23),
        footer_text,
        font=footer_font,
        fill=colors["muted"],
    )

    image.save(save_path, "PNG")
