# 声匣 Soundbox

声匣是一款面向游戏创作者的本地音效工作台。你可以用中英文寻找声音、试听并整理素材，也可以在同一个界面里截取、拼接和导出音频。Windows 下独立运行，不需要安装创作工具 Hub。

目前提供的是**源码运行方式**：首次使用需要自行安装依赖并构建界面。本仓库没有把其他人的商业素材包或个人素材库打包进来。

## 能做什么

- 搜索精选开放游戏音效目录，也可切换 Openverse、Freesound 或自己的本地素材。精选目录的索引可离线搜索，首次试听未缓存的音效时会从其官方来源下载相应素材包；Openverse 可匿名搜索，Freesound 直连需要自己的 API Key。
- 试听、下载、收藏、添加标签和备注、创建文件夹分类。可建立本地 ZIP 音效包索引，不需要将整个包解压到素材库。
- 拖入视频提取第一条音轨，在本机截取片段，拼接素材，调整音量与淡入淡出，并试听、另存。支持直线、先慢后快、先快后慢和 S 形淡变。视频中的人声、音乐和音效不会自动分离。
- 编辑结果可下载为 WAV，供手动导入 DAW；此功能不会自动打开或操控 DAW，尚未在实际 DAW 软件中验收。
- 可选配置 DeepSeek 或其他兼容文本 API，使用自然语言整理检索词或请求标签建议；还可自行配置 ElevenLabs 生成音效。调用各服务可能产生费用，文本 AI 搜索默认关闭。标签建议基于文字，不会聆听音频。
- 对音频做客观电平检查、隐藏和音量补偿。检查不能判断主观音质或是否适合具体游戏，仍需要亲自试听。

## Windows 便携包状态

目前尚无公开下载的声匣 Windows 便携包；请勿将 GitHub 自动生成的源码压缩包当作可直接运行的便携包。本仓库提供便携包构建源码以供审查和开发者测试：`tools/build_standalone_portable.py` 使用另行准备的 Windows x64 运行时，不自动下载运行时，也不包含运行时。构建前还需 Windows .NET Framework C# 编译器、Python、Node.js 和构建好的前端。

外部运行时必须含通过工具校验的 `files.json` 清单及 Python、FFmpeg 组件；运行时的准备方式及对应许可材料尚未作为本仓库发布内容完成，因此构建脚本目前不是免配置安装器。FFmpeg 的分发义务见 [第三方来源与许可说明](THIRD_PARTY.md)。

## Windows 源码运行

准备 [Python 3.11 或更新版本](https://www.python.org/downloads/windows/)、[Node.js](https://nodejs.org/)（建议 20 或更新的 LTS 版本）以及 [FFmpeg](https://ffmpeg.org/download.html)，并确保 `python`、`npm`、`ffmpeg` 和 `ffprobe` 可在终端中找到。FFmpeg 的剪辑与音频检查功能需要同时能调用 `ffmpeg` 和 `ffprobe`。安装 Python 时可勾选添加到 PATH；安装后重新打开终端。

下载本仓库源码并解压，或用 Git 克隆；在**仓库根目录**打开 PowerShell，执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd ci --prefix web
npm.cmd run build --prefix web
```

构建完成后，双击仓库根目录的 **启动声匣.vbs**。启动器在后台打开本地服务和浏览器；默认端口是 `23176`，端口占用时会自动寻找可用端口，重复启动会复用兼容实例。若误关闭浏览器页面，可再次双击启动器。前端源码改变后，需要重新执行 `npm.cmd run build --prefix web`。

如果提示缺少 Python、FFmpeg 或依赖，请检查上述程序是否安装并已加入 PATH，重新执行安装命令；启动日志位于项目的 `data/logs/server.log`。网络搜索及首次下载开放素材需要网络，服务是否可用由各提供方决定。

## 开始使用

1. 在搜索框输入中文或英文，选择需要的来源，再试听合适的结果；联网来源的预览文件可能不是原作者上传的原始格式。
2. 点击爱心收藏，或用“加入文件夹”保存和分类；可在素材详情编辑标签与备注。
3. 点击素材的“剪辑”，或直接将视频拖入页面。选择范围、调整淡变并生成试听，确认后保存新音效或下载 WAV。
4. 需要自然语言整理、标签建议或 AI 音效生成时，先在“来源与 AI 设置”配置自己的服务；搜索框旁的 AI 开关可以随时关闭，以避免不必要的文本 API 调用。

空格或 `K` 可播放/暂停；编辑器参数改变后需重新生成试听。完整快捷键可在应用内查看。

## 数据和素材许可

素材库、下载和配置保存在本机 `data/`。Windows 的服务密钥使用当前账户的 DPAPI 加密，但**请勿公开上传或分享整个 `data/` 目录**。备份前先停止服务，再复制整个数据目录；迁移账户或电脑后可能需要重新填写密钥。

本项目代码使用 [MIT 许可证](LICENSE)。音效、第三方软件和在线服务有各自的许可与条款：素材的作者、来源、许可及署名要求以每个素材实际信息为准，免费不等于可以随意再分发。视频提取结果沿用原视频及生成平台的使用授权。详情见 [第三方来源与许可说明](THIRD_PARTY.md)。

## 开发与测试

安装开发依赖后运行测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:SOUNDBOX_DATA_DIR = Join-Path $env:TEMP 'soundbox-test-data'
$env:SOUNDBOX_NO_SEED = '1'
.\.venv\Scripts\python.exe -m pytest tests -q
```

运行测试和开发实验时请使用独立数据目录，不要把自己的素材库作为测试夹具。测试过程中如需启动服务，也请避开正在使用的实例和端口。此项目当前主要面向本机单用户使用；云同步、多人账号及服务端收费系统不在当前功能范围内。
