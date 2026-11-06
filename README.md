# 落月点歌 · astrbot_plugin_luoyue_music

> 基于 [落月 API](https://doc.vkeys.cn) 的 AstrBot 点歌插件，支持网易云音乐 / QQ 音乐搜索、候选选歌、音乐卡片、语音、文件与播放链接。

![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.10.4-blue)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![License](https://img.shields.io/badge/License-MIT-green)

## ✨ 特性

- 🎵 **双音源**：网易云音乐、QQ 音乐，可在聊天中用 `/点歌源` 随时切换
- 🔍 **搜索选歌**：一次返回多首候选，回复序号即可播放，候选默认 120 秒有效
- ⚡ **直接点歌**：`/直接点歌 歌名` 命中第一首直接播放
- 🃏 **多种发送方式**：音乐卡片、播放链接、语音条、音频文件；既可设为默认，也可在点歌时临时指定
- 🎚 **音质可调**：网易云 1-9、QQ 音乐 0-16 级音质上限
- 🧩 **开箱即用**：无需申请 Token，安装后在 WebUI 配置即可使用
- 🔁 **跨音源兜底**：当前音源取不到播放链接时，自动到另一音源搜索同名歌曲
- 🛡 **稳健处理**：接口超时重试、异常兜底、并发锁、候选缓存自动清理

## 📦 安装

### 方式一：AstrBot 插件市场

在 WebUI 的「插件」页面搜索 `落月点歌` / `astrbot_plugin_luoyue_music`，点击安装。

### 方式二：手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/xZhongjie/astrbot_plugin_luoyue_music.git
```

重启 AstrBot，或在 WebUI 插件页点击「重载插件」。

依赖：`aiohttp`（AstrBot 已内置，`requirements.txt` 中仅作声明）。

## 🚀 使用

| 指令 | 说明 |
| --- | --- |
| `/点歌 歌名` | 搜索歌曲并列出候选 |
| `/点歌 序号` | 播放候选中对应序号的歌曲 |
| `/选歌 序号` | 同上（别名 `/选`） |
| `/直接点歌 歌名` | 直接播放搜索结果第一首（别名 `/点播`、`/来一首`） |
| `/语音点歌 歌名` | 直接以语音条发送（别名 `/语音点播`） |
| `/文件点歌 歌名` | 直接以音频文件发送（别名 `/文件点播`） |
| `/点歌源` | 查看当前音源 |
| `/点歌源 网易云` | 切换为网易云音乐 |
| `/点歌源 QQ音乐` | 切换为 QQ 音乐 |
| `/点歌帮助` | 显示帮助 |

以上指令都可以在末尾追加发送方式，临时覆盖配置：

```text
/点歌 晴天 语音
/点歌 晴天 文件
/点歌 晴天 卡片
/点歌 晴天 链接
/选歌 1 语音
/直接点歌 晴天 文件
```

支持的发送方式关键词：`语音` / `voice` / `record`、`文件` / `file`、`卡片` / `card`、`链接` / `link`。

### 示例

```text
用户：/点歌 晴天
Bot：🎵 落月点歌 · QQ 音乐
      🔍 搜索：晴天（共 5 首）

      1. 晴天 - 周杰伦
         《叶惠美》 · HQ高音质 · 付费
      2. 晴天 - 沈以诚
      ...

      回复「/点歌 序号」或「/选歌 序号」播放，120 秒内有效。

用户：/点歌 1
Bot：🎵 晴天
     👤 歌手：周杰伦
     💿 专辑：叶惠美
     🎧 音乐试听 · ⏱ 4分29秒 · 0.92MB
     🔗 https://i.y.qq.com/v8/playsong.html?songmid=0039MnYb0qxYhV&type=0
     [音乐卡片]
```

> 候选列表按「平台 + 用户」隔离，每个人选择的是自己搜索到的结果。

### 语音 / 文件发送

```text
用户：/语音点歌 晴天
Bot：🎵 晴天（歌曲信息）
     [语音条]

用户：/文件点歌 晴天
Bot：🎵 晴天（歌曲信息）
     [文件：晴天 - 周杰伦.mp3]

用户：/点歌 晴天 文件
Bot：候选列表（本次发送方式：文件）
用户：/点歌 1
Bot：[文件]
```

也可以把 `send_mode` 配置为 `record` 或 `file`，让所有点歌默认使用语音 / 文件发送。

> 语音模式需要 AstrBot 所在机器安装 `ffmpeg`（AstrBot 会把音频转码为 wav 再交给协议端）。文件模式会把音频以文件消息发送，保留原始格式（mp3 / flac / m4a 等）。
>
> QQ 语音条通常限制 60 秒，超过 `voice_max_seconds` 的歌曲会自动改为发送音频文件；如果想强制语音，可把该值调大或设为 0。未检测到 ffmpeg 时，语音模式会自动降级为播放链接。

## ⚙️ 配置

配置文件位于 WebUI「插件」→「落月点歌」→「配置」，对应 `data/config/astrbot_plugin_luoyue_music_config.json`。

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `api_base` | string | `https://api.vkeys.cn` | 落月 API 地址，备用域名 `https://api.epdd.cn` |
| `default_source` | string | `tencent` | 默认音源：`tencent` / `netease`（当前网易云线路可能取不到链接，推荐 QQ 音乐） |
| `search_limit` | int | `5` | 搜索结果数量（1-10） |
| `quality_netease` | int | `3` | 网易云音质上限（1-9，实际上限以 API 为准，超限自动降级） |
| `quality_tencent` | int | `8` | QQ 音乐音质上限（0-16） |
| `send_mode` | string | `auto` | 默认发送方式：`auto` / `card` / `link` / `record` / `file` |
| `auto_play_single` | bool | `true` | 只命中一首时直接播放 |
| `auto_source_fallback` | bool | `true` | 取不到播放链接时自动到另一音源搜索同名歌曲 |
| `voice_max_seconds` | int | `60` | 语音时长上限（秒），超出后自动改发音频文件，0 表示不限制 |
| `selection_ttl` | int | `120` | 候选有效期（秒） |
| `request_timeout` | int | `15` | 接口超时（秒） |
| `proxy` | string | 空 | 请求落月 API 时使用的 HTTP 代理 |
| `show_cover` | bool | `false` | 链接模式是否附带封面图 |

### 音质对照

**网易云音乐**：1=标准 64k，2=标准 128k，3=HQ 192k，4=HQ 320k，5=无损，6=Hi-Res，7=高清臻音，8=沉浸环绕声，9=超清母带。当前落月 API 的网易云线路实际只允许 1-3，插件会自动降级。

**QQ 音乐**：0=试听，1-3=有损，4-7=标准，8-9=HQ，10=无损，11=Hi-Res，12=杜比全景声，13=臻品全景声，14=臻品母带 2.0，15-16=AI 音轨（不建议）。

### 发送方式说明

| 模式 | 行为 | 适用平台 |
| --- | --- | --- |
| `auto` | OneBot 平台发音乐卡片，其他平台发链接 | 全部 |
| `card` | OneBot `music` 消息段：网易云用 `163` 卡片，QQ 音乐用 `qq` 卡片，失败时退回 `custom` 卡片 | aiocqhttp / NapCat / Lagrange |
| `link` | 发送歌曲信息 + 播放页链接 | 全部 |
| `record` | 以语音条发送 | aiocqhttp（需要 ffmpeg 转码） |
| `file` | 以音频文件发送 | 支持文件消息的平台 |

> `record` 模式会在 AstrBot 所在机器下载音频并转码为 wav，请确保已安装 `ffmpeg`，且音频时长不要过长。

## ❓ 常见问题

**Q：提示「该歌曲暂时没有可用的播放链接」？**

A：可能是版权 / VIP 限制，或接口线路临时不可用。插件默认会到另一音源搜索同名歌曲并自动播放；如果两边都取不到，会附上歌曲播放页链接。也可以手动 `/点歌源` 切换音源或换一首歌。落月 API 会缓存 5 分钟至 1 天的数据，重复请求可能拿到同样的结果。

**Q：网易云歌曲总是取不到链接？**

A：落月 API 的网易云线路可能暂时不返回 `url`（搜索正常但播放地址为空）。插件会自动尝试用 QQ 音乐播放同名歌曲；也可以发送「/点歌源 QQ音乐」手动切换，或在配置中把 `default_source` 改为 `tencent`。

**Q：音乐卡片在 QQ 里点不开 / 显示过期？**

A：原生卡片依赖 QQ 音乐 / 网易云客户端与版权状态。可把 `send_mode` 改为 `link` 或 `file` 作为兜底，链接为播放页地址。

**Q：语音条发送失败？**

A：确认 AstrBot 运行环境已安装 `ffmpeg`，并检查日志中的转码错误。

**Q：需要填 API Key 吗？**

A：落月 API v2 的公开接口不需要鉴权，插件也无需 Token。若上游未来调整策略，可在 `api_base` 中换成兼容的自建地址。

## 🔗 相关链接

- 落月 API 文档：<https://doc.vkeys.cn>
- AstrBot 项目：<https://github.com/AstrBotDevs/AstrBot>
- 插件仓库：<https://github.com/xZhongjie/astrbot_plugin_luoyue_music>

## 📄 声明

- 本插件仅调用第三方公开 API 完成搜索与取链，不存储、不分发任何音频文件。
- 音乐版权归各唱片公司及平台所有，请勿用于商业用途。
- 使用本插件产生的 API 调用行为请遵守落月 API 的服务条款。

## License

[MIT](LICENSE)
