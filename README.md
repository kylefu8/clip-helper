# 片段助手

**v1.0.0 · Windows 64 位桌面工具**

手动选出视频中要保留的片段，先预览裁剪结果，再导出新文件或确认替换原片。适合整理钓鱼素材，也可用于其他视频。视频仅在本机处理，无需上传。

## 下载与启动

在 GitHub Releases 下载 `clip-helper-v1.0.0-windows-x64.zip`，完整解压后双击 `片段助手.exe`。请保留旁边的 `_internal` 文件夹，无需安装 Python。建议使用 Windows 10/11 64 位。

## 快速使用

1. 选择目录，在左侧选择视频。
2. 播放或拖动时间轴，用 **I** 标开始、**O** 标结束。
3. **点击“添加保留片段”**。一个视频需要保留多段时，逐段添加。
4. 点击“准备已标记的视频”，等待处理和校验完成。
5. 回看结果，再选择导出新文件或确认替换原片。

空格控制播放/暂停；播放按钮两侧可逐帧查看。标记自动保存，刷新会保留未变化文件的标记。移除列表项只隐藏视频，不删除文件；可以恢复。

软件不重新压缩画面。每段开始会向前对齐到可独立解码的关键帧，可能多保留一点开头，实际范围会显示在结果中。多段按原时间顺序连接。暂存结果需要额外空间。

**确认替换会永久移除未保留的画面，成功后不留原片副本。** 需要保留原片时请选择导出新文件。更多说明见 [使用说明.md](使用说明.md)。

## 从源码运行

此版本使用 Python 3.14.2 构建和验证。源码运行需要 Python，以及本机 FFmpeg、ffprobe 和 GPAC 的 MP4Box。将媒体工具加入 PATH，或放入 `tools/ffmpeg.exe`、`tools/ffprobe.exe`、`tools/gpac/mp4box.exe`（连同其依赖动态库）。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

运行测试：`python -m unittest discover -s tests -v`。构建 Windows 文件夹：`python build.py`；构建前需准备上述 `tools` 目录。版本号统一定义在 `version.py`。

本仓库只包含软件源码、合成素材测试、图标和通用说明；不包含用户视频、标记、缓存或运行记录。第三方组件说明与许可证见 [licenses/NOTICE.txt](licenses/NOTICE.txt)。
