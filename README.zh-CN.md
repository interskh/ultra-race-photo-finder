# ultra-race-photo-finder

[English](README.md)

在几万张赛事照片里找到你自己：即使脸被遮住、在夜间拍摄也能找。可以按衣着、号码布、时间、摄影师或场景（"雪山"、"终点拱门"）搜索，把找到的照片标记成"是我"，再让它找出更多。专为国内越野跑和超马赛事常用的照片直播平台打造：一拍即传（yipai360）、拍立享（pailixiang）、享像派（xxpie）和 PhotoPlus（谱时）。全部在本地 Mac 上运行，不需要账号，也不上传到云端。网页界面目前是英文的，下文按钮名保留英文原文。

命令行工具和 Python 包名为 `photofinder`。

## 工作原理

1. **下载**：抓取赛事公开相册的预览图，并有意放慢请求，不给网站造成压力。
2. **建索引**：在本地完成人物检测（YOLO）、衣着重识别（OSNet）、图像/文本向量（SigLIP2），可选号码布 OCR（Apple Vision）。
3. **搜索**：在浏览器里操作。从你的号码布、一张自己的照片或一句描述开始，把结果标成 **✓ Me**（是我）/ **✗ Not me**（不是我），再点 **Find more like my marked ones**（找更多像我标记的），其他摄影师拍到你的照片就会冒出来。在一张确认是你的照片前后几秒拍的连拍，也会列在它旁边。
4. **导出**：一份 CSV、网站免费提供的全尺寸原图，或打包成 zip。

几百名跑者穿着同款赛事外套时，单靠衣着相似度效果有限。真正管用的是这个循环：号码布 → 标记 → 找更多 → 再标记。实测数据见[设计记录](docs/handoff/2026-09-27-photo-finder-search-design.md)（英文）。

## 支持的相册平台

| 平台 | 相册网址 | 下载的预览图 | 原图（只针对你标记的照片） |
|---|---|---|---|
| 一拍即传 yipai360 | `www.yipai360.com/…?orderId=…` | 1920 px，带水印 | 全尺寸、带 EXIF，与网站"下载"按钮得到的相同（部分相册会加主办方的品牌条） |
| 拍立享 pailixiang | `live.pailixiang.com/album/<id>` | 1600 px | 免费的全尺寸带水印版本，无 EXIF |
| 享像派 xxpie | `www.xxpie.com/m/album?id=<id>` | 约 2560 px，带水印 | 免费的全尺寸带水印版本，带 EXIF |
| PhotoPlus 谱时 | `live.photoplus.cn/live/<id>` | 1600 px，带水印 | 免费的全尺寸版本，带摄影师 logo 水印 |

各平台的无水印原图都需要付费。每张照片都有 **Open on site**（在网站打开）链接，可以直接去相册里购买。

## 运行环境

- Apple Silicon 芯片的 Mac（模型跑在 MPS 上；号码布 OCR 用 Apple Vision）。开发机为 16 GB 内存；建索引时每个模型阶段默认最多占用 4 GB。
- [uv](https://docs.astral.sh/uv/)（Python 3.13 由 uv 自动安装）。
- 磁盘空间：模型权重约 1.5 GB（首次使用时下载）；预览图加索引大约每 1000 张照片 0.6 GB。一场 12000 张照片的赛事约占 7 GB。

## 安装

```
git clone <本仓库地址> ultra-race-photo-finder
cd ultra-race-photo-finder
uv sync
```

所有数据都放在仓库目录下的 `data/`（已被 git 忽略）。如果想放到别处（比如外接硬盘），设置 `PHOTOFINDER_DATA_ROOT=/path/to/data`。

## 快速开始

```
uv run photofinder race add 2026-myrace "2026 我的比赛"                     # 一场比赛可以包含多个相册
uv run photofinder album add 2026-myrace https://live.pailixiang.com/album/a13800138000
scripts/download.sh 2026-myrace                                           # 后台运行、可断点续传；过几天再跑一次可补充新上传的照片
tail -f data/races/2026-myrace/download-console.log
uv run photofinder index 2026-myrace                                      # 加 --ocr 识别号码布（较慢）
uv run photofinder serve                                                  # http://127.0.0.1:8000/
```

建索引的时间取决于赛事规模。在 M4 Mac 上，约 96000 张照片用了 4 小时 37 分钟。索引是增量的：补充下载后再跑一次 `index`，只处理新照片。

在浏览器里选好比赛，输入你的号码布，或上传一张自己的照片并点选照片里的你。完整说明见 [docs/usage.md](docs/usage.md)（英文）。

## 数据目录

```
data/races.json                                         比赛登记表（由命令行写入）
data/races/<race>/index.sqlite                          每场比赛一个索引
data/races/<race>/albums/<platform>-<id>/photos/        下载的预览图
data/races/<race>/albums/<platform>-<id>/manifest.sqlite  相册目录：摄影师、拍摄时间、分组
data/exports/<race>/<person>/{originals/, photos.csv}   你的导出文件
data/models/                                            模型权重和缓存
```

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `PHOTOFINDER_DATA_ROOT` | `<仓库目录>/data` | 比赛登记表、比赛数据、导出、服务锁文件 |
| `PHOTOFINDER_MODELS_DIR` | `<仓库目录>/data/models` | 模型权重；不受 `PHOTOFINDER_DATA_ROOT` 影响 |

## 请友善对待这些网站

下载器只抓取访客在浏览器里本来就能看到的内容，而且刻意限速：少量并发、翻页间隔、熔断机制，查找原图每隔几秒才发一次请求。请保持这些设置。请只用它找你自己的照片（或经朋友同意后找他们的），尊重摄影师的版权和各网站的使用条款，想保留的无水印照片请购买。这些网站的内部接口随时可能变化，届时对应的下载器会失效。

## 文档

- [docs/usage.md](docs/usage.md)：网页界面和命令行的详细说明。
- [docs/superpowers/specs/](docs/superpowers/specs/)：设计文档（架构、数据模型、打分、比赛与相册）。
- [docs/handoff/](docs/handoff/)：开发记录，包括决策、放弃的方案和实测结果。
- [docs/ROADMAP.md](docs/ROADMAP.md)：进度、下一步计划和已知问题。

以上文档均为英文。

## 开发

```
uv run pytest -q
```

测试使用模拟的 HTTP 服务和替身模型，不需要联网，也不需要模型权重。设置 `PHOTOFINDER_REAL_MODELS=1` 会启用一个加载真实模型的测试。`CLAUDE.md` 是给编程智能体的工作规则。

## 许可证

[AGPL-3.0-or-later](LICENSE)。其中的人物检测（Ultralytics YOLO）和重识别库（BoxMOT）均为 AGPL-3.0 许可。
