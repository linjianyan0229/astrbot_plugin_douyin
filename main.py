from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
import httpx
import re


DOUYIN_URL_PATTERN = re.compile(r'https?://v\.douyin\.com/[A-Za-z0-9_\-]+/?')
PARSE_API = "https://toody.netlify.app/.netlify/functions/parse"


@register("astrbot_plugin_douyin", "linjianyan0229", "自动检测抖音分享链接，解析并发送无水印视频", "1.0.0")
class DouyinPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-1)
    async def on_message(self, event: AstrMessageEvent):
        # 群组白名单检查
        group_id = event.message_obj.group_id
        if group_id:
            whitelist = self.config.get("group_whitelist", [])
            if whitelist and str(group_id) not in [str(g) for g in whitelist]:
                return
        else:
            # 私聊消息
            if not self.config.get("enable_in_private", True):
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
            yield event.plain_result(f"抖音解析成功但暂不支持该内容类型: {content_type}")

    async def terminate(self):
        pass
