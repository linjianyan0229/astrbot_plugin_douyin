import datetime
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .account_balance import AccountBalanceResult
from .billing_rate import BillingRateResult


_BUNDLED_FONT = Path(__file__).parent / "DouyinSansBold.otf"
_SHANGHAI_TZ = datetime.timezone(datetime.timedelta(hours=8))


@dataclass(frozen=True)
class UpstreamStatusResult:
    station_name: str
    balance: AccountBalanceResult
    billing: BillingRateResult


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


def _money(value: float | None, protocol: str) -> str:
    if value is None:
        return "-"
    precision = 8 if protocol == "sub2api" else 6
    return f"${value:.{precision}f}"


def _rate(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.4f}".rstrip("0").rstrip(".") + "x"


def render_upstream_status(
    results: list[UpstreamStatusResult],
    save_path: str | Path,
):
    if not results:
        raise ValueError("没有可渲染的上游数据")

    width = 1360
    margin = 44
    title_h = 130
    header_h = 58
    row_h = 92
    footer_h = 58
    table_top = title_h
    body_top = table_top + header_h
    height = body_top + len(results) * row_h + footer_h

    colors = {
        "bg": (244, 246, 248),
        "panel": (255, 255, 255),
        "header": (30, 34, 42),
        "ink": (30, 34, 41),
        "muted": (112, 119, 132),
        "line": (221, 225, 231),
        "green": (23, 145, 103),
        "orange": (215, 126, 35),
        "blue": (43, 113, 246),
        "yellow": (205, 137, 25),
        "red": (210, 67, 78),
    }

    image = Image.new("RGB", (width, height), colors["bg"])
    draw = ImageDraw.Draw(image)
    title_font = _get_font(38)
    subtitle_font = _get_font(16)
    header_font = _get_font(16)
    name_font = _get_font(19)
    body_font = _get_font(18)
    detail_font = _get_font(13)
    footer_font = _get_font(14)

    draw.text((margin, 36), "上游状态", font=title_font, fill=colors["ink"])
    draw.text(
        (margin, 90),
        "余额与分组倍率",
        font=subtitle_font,
        fill=colors["muted"],
    )
    draw.text(
        (width - margin, 70),
        f"共 {len(results)} 个上游",
        font=subtitle_font,
        fill=colors["muted"],
        anchor="rm",
    )

    x0 = margin
    x1 = 304
    x2 = 524
    x3 = 774
    x4 = 904
    x5 = 1124
    x6 = width - margin
    draw.rounded_rectangle(
        (x0, table_top, x6, height - footer_h + 2),
        radius=8,
        fill=colors["panel"],
    )
    draw.rounded_rectangle(
        (x0, table_top, x6, body_top),
        radius=8,
        fill=colors["header"],
    )
    draw.rectangle((x0, body_top - 8, x6, body_top), fill=colors["header"])

    headers = (
        ("中转站", x0 + 22, "lm"),
        ("余额", x1 + 20, "lm"),
        ("累计消耗", x2 + 20, "lm"),
        ("次数", x4 - 20, "rm"),
        ("分组", x4 + 20, "lm"),
        ("倍率", x6 - 22, "rm"),
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
        balance = result.balance
        billing = result.billing
        top = body_top + index * row_h
        center_y = top + row_h // 2
        if index:
            draw.line((x0 + 18, top, x6 - 18, top), fill=colors["line"], width=1)
        if index % 2:
            draw.rectangle((x0, top, x6, top + row_h), fill=(250, 251, 253))

        balance_ok = balance.success
        billing_ok = billing.effective_rate_multiplier is not None
        if balance_ok and billing_ok:
            accent = colors["green"]
        elif balance_ok or billing_ok or billing.state == "degraded":
            accent = colors["yellow"]
        else:
            accent = colors["red"]
        draw.rectangle((x0, top, x0 + 6, top + row_h), fill=accent)

        name = _ellipsize(draw, result.station_name, name_font, x1 - x0 - 50)
        draw.text(
            (x0 + 22, center_y - 10),
            name,
            font=name_font,
            fill=colors["ink"],
            anchor="lm",
        )
        protocol_text = balance.mode or balance.protocol.replace("_", " ")
        protocol_text = _ellipsize(
            draw, protocol_text, detail_font, x1 - x0 - 50
        )
        draw.text(
            (x0 + 22, center_y + 18),
            protocol_text,
            font=detail_font,
            fill=colors["muted"],
            anchor="lm",
        )

        if balance_ok:
            balance_text = (
                "无限额度"
                if balance.unlimited
                else _money(balance.balance_usd, balance.protocol)
            )
            draw.text(
                (x1 + 20, center_y),
                balance_text,
                font=body_font,
                fill=colors["green"],
                anchor="lm",
            )
            used_y = center_y - 10 if balance.today_used_usd is not None else center_y
            draw.text(
                (x2 + 20, used_y),
                _money(balance.used_usd, balance.protocol),
                font=body_font,
                fill=colors["orange"],
                anchor="lm",
            )
            if balance.today_used_usd is not None:
                draw.text(
                    (x2 + 20, center_y + 18),
                    f"今日 {_money(balance.today_used_usd, 'sub2api')}",
                    font=detail_font,
                    fill=colors["muted"],
                    anchor="lm",
                )
            count_y = center_y - 10 if balance.request_period else center_y
            draw.text(
                (x4 - 20, count_y),
                str(balance.request_count),
                font=body_font,
                fill=colors["blue"],
                anchor="rm",
            )
            if balance.request_period:
                draw.text(
                    (x4 - 20, center_y + 18),
                    balance.request_period,
                    font=detail_font,
                    fill=colors["muted"],
                    anchor="rm",
                )
        else:
            draw.text(
                (x1 + 20, center_y - 10),
                "查询失败",
                font=body_font,
                fill=colors["red"],
                anchor="lm",
            )
            detail = _ellipsize(
                draw, balance.detail, detail_font, x2 - x1 - 40
            )
            draw.text(
                (x1 + 20, center_y + 18),
                detail,
                font=detail_font,
                fill=colors["muted"],
                anchor="lm",
            )
            draw.text((x2 + 20, center_y), "-", font=body_font, fill=colors["muted"], anchor="lm")
            draw.text((x4 - 20, center_y), "-", font=body_font, fill=colors["muted"], anchor="rm")

        group_name = billing.group_name
        if not group_name and billing.protocol == "sub2api":
            group_name = "实时倍率"
        if not group_name:
            group_name = "未知"
        group_name = _ellipsize(draw, group_name, body_font, x5 - x4 - 40)
        draw.text(
            (x4 + 20, center_y - 10),
            group_name,
            font=body_font,
            fill=colors["ink"],
            anchor="lm",
        )
        billing_detail = _ellipsize(
            draw, billing.detail, detail_font, x5 - x4 - 40
        )
        draw.text(
            (x4 + 20, center_y + 18),
            billing_detail,
            font=detail_font,
            fill=colors["muted"],
            anchor="lm",
        )

        rate_value = billing.effective_rate_multiplier
        rate_color = (
            colors["green"]
            if billing.state == "healthy"
            else colors["yellow"]
            if billing.state == "degraded"
            else colors["red"]
        )
        rate_text = _rate(rate_value) if rate_value is not None else "查询失败"
        draw.text(
            (x6 - 22, center_y - 10),
            rate_text,
            font=body_font,
            fill=rate_color,
            anchor="rm",
        )
        if billing.last_rate_multiplier is not None:
            last_text = f"上次 {_rate(billing.last_rate_multiplier)}"
        else:
            last_text = "当前最终倍率" if rate_value is not None else "-"
        draw.text(
            (x6 - 22, center_y + 18),
            last_text,
            font=detail_font,
            fill=colors["muted"],
            anchor="rm",
        )

    checked_at = datetime.datetime.now(_SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    draw.text(
        (margin, height - footer_h // 2),
        "金额 USD  |  倍率 x",
        font=footer_font,
        fill=colors["muted"],
        anchor="lm",
    )
    draw.text(
        (width - margin, height - footer_h // 2),
        f"查询时间 {checked_at}",
        font=footer_font,
        fill=colors["muted"],
        anchor="rm",
    )
    image.save(save_path, "PNG", optimize=True)
