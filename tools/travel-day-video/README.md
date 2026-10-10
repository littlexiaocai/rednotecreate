# 旅行每日 Vlog 生成器

把每一天拍的照片剪成一条 720p 小视频（Day 1、Day 2 …），用来保存旅行回忆。

- 素材：SD 卡里的 **JPG**（RAW 不读）+ Mac「照片」图库里同一天的 **JPG**，按拍摄时间合并排序
- 去重：SD 卡的照片如果已经导入过照片图库，只保留一份
- 画面：片头卡（DAY N / 日期星期 / 副标题）→ 每张照片缓慢推拉 + 叠化转场 → 片尾卡淡出
- 竖图放在相纸边框里，背景用模糊放大的原图；整体轻微暖调、暗角，右下角有橙色胶片日期戳
- 配乐：用 `--music` 指定自己的歌；不指定时会自动合成一段免版权的氛围配乐（每天的调和和弦走向都不一样）
- 输出：1280×720、30fps、H.264 + AAC，默认存到 `~/Movies/川渝Vlog/Day01_2026-10-01.mp4`

## 第一次使用（Mac 上只需要做一次）

```bash
brew install ffmpeg
pip3 install pillow numpy osxphotos
```

读取「照片」图库需要给终端「完全磁盘访问权限」：
系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 把「终端」（或你用的 iTerm 等）打开。

## 做 Day 1

插好 SD 卡（`/Volumes/PP02`），然后：

```bash
cd tools/travel-day-video
python3 make_day_video.py --day 1 --list     # 先看看会用到哪些照片（可选）
python3 make_day_video.py --day 1            # 渲染 Day 1
open ~/Movies/川渝Vlog
```

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--day 3` | 做第 3 天（Day 1 = `--start` 那天，默认 2026-10-01） |
| `--all` | 9 天一次全做完 |
| `--title "川渝 · 重庆"` | 片头副标题，默认「川渝旅行」 |
| `--music ~/Music/vlog` | 指定配乐文件，或一个文件夹（Day 1 用第 1 首、Day 2 用第 2 首…） |
| `--sec 3` | 每张照片停留秒数（默认按照片数量自动在 2.2–3.5 秒之间） |
| `--include-heic` | 照片图库里 iPhone 拍的 HEIC 也转成 JPG 一起用（默认只用 JPG） |
| `--icloud` | 原图只存在 iCloud 上的照片也尝试下载 |
| `--sd-offset-min 8` | 相机时钟比手机慢 8 分钟时用来校正，保证两边照片顺序正确 |
| `--no-library` / `--no-sd` | 只用其中一边的素材 |
| `--out 路径` | 输出文件夹 |
