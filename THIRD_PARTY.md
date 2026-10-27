# 第三方来源与许可

本仓库自身代码按根目录的 [MIT 许可证](LICENSE) 发布。MIT 不覆盖第三方软件、第三方音频、在线服务或用户导入的内容；使用和分发时，请分别核对其适用许可、署名要求以及服务条款。

## 音频目录及在线服务

- 精选开放目录收录了来自 [Kenney](https://kenney.nl/assets) 和 [OpenGameArt](https://opengameart.org/) 等作者页面的固定素材包。目录清单标记的素材按所记录的 [CC0](https://creativecommons.org/publicdomain/zero/1.0/) 来源提供；作者、原始作品页、许可与原包校验信息保存在 `server/catalog_manifest.json` 和 `server/gamepacks_manifest.json`。素材由声匣按需从官方来源获取，实际音质和格式因素材而异。
- [Openverse](https://openverse.org/) 是开放内容搜索索引。其结果可能属于不同作者和许可；请在使用前查看每条结果的来源页、许可及署名信息。Openverse 试听文件不一定是上传者的原始无损音频。
- [Freesound](https://freesound.org/) 的每条音频许可可能不同；其 [API 条款](https://freesound.org/help/tos_api/) 与素材许可是不同的要求。Freesound 搜索需要自行申请 API 凭证，原文件下载还需要相应账号授权；商业接入请自行核对服务条件。
- 个人下载的免费赠品、本地 ZIP、导入的视频、AI 生成音频，以及音频编辑的成品，均依其原始来源和服务条款处理。免费使用、可用于商业项目和允许重新分发并不等同；本仓库不授予这些第三方素材的额外权利。
- [DeepSeek](https://api-docs.deepseek.com/)、[ElevenLabs](https://elevenlabs.io/docs/) 等可选服务由用户自行配置凭据，使用时适用各服务的条款与费用；本仓库不包含 API Key，也不承诺免费额度。

## 程序依赖

Python 运行依赖及固定版本见 [`requirements.txt`](requirements.txt)，开发测试依赖见 [`requirements-dev.txt`](requirements-dev.txt)；前端依赖及锁定版本见 [`web/package.json`](web/package.json) 和 [`web/package-lock.json`](web/package-lock.json)。这些依赖分别依其上游项目的许可证提供。安装工具下载的第三方依赖不因本仓库的 MIT 许可证而改变许可。

音频处理需要用户自行安装 [FFmpeg](https://ffmpeg.org/)。FFmpeg 的实际许可取决于具体构建和启用的组件；如需再分发 FFmpeg，请检查所选构建的许可并遵守其要求。本仓库源码不包含 FFmpeg 可执行文件。

Windows 便携包使用单独构建的 FFmpeg 8.0.1（LGPL 2.1 或更新版本）及 LAME 3.100（LGPL 2 或更新版本）共享库；两者不按本项目 MIT 许可分发。许可文本、具体构建配置和来源信息随运行时提供；对应源码归档及构建脚本可在 [v0.1.0 发布页](https://github.com/JamStrak/soundbox/releases/tag/v0.1.0) 下载。构建未启用 FFmpeg 的 GPL 或 nonfree 选项。Python 嵌入运行时及其第三方包的版本和许可说明见便携包 `runtime/versions.json` 和相应许可证文件。
