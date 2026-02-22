# astrbot_plugin_douyin

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件，自动检测聊天中的抖音分享链接，解析并发送无水印视频/图集。

## 功能

- 自动识别消息中的抖音分享链接（`v.douyin.com`）
- 调用解析 API 获取无水印视频/图集
- 支持**视频**类型：发送作者、标题信息 + 无水印视频
- 支持**图集**类型：发送作者、标题信息 + 所有图片

## 使用方式

无需任何命令，直接在群聊或私聊中发送包含抖音链接的消息即可自动触发：

```
8.76 EUl:/ o@D.Hv 01/30 # gentleman # 卡点舞 # ootd穿搭  https://v.douyin.com/Peo6RZAd-Dk/ 复制此链接，打开Dou音搜索，直接观看视频！
```

插件会自动提取链接并回复解析结果。

## 安装

通过 AstrBot WebUI 插件管理界面安装，或手动克隆到插件目录：

```bash
cd data/plugins
git clone https://github.com/linjianyan0229/astrbot_plugin_douyin.git
```

## 依赖

- [httpx](https://www.python-httpx.org/) — 异步 HTTP 客户端

## 相关链接

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
