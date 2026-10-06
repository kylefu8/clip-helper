"""Find the application's bundled local media tools."""
import shutil
import sys
from pathlib import Path


def resource_directory() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def resolve_tools() -> dict[str, str]:
    root = resource_directory()
    result = {}
    for name, relative in (("ffmpeg", "tools/ffmpeg.exe"), ("ffprobe", "tools/ffprobe.exe"),
                           ("mp4box", "tools/gpac/mp4box.exe")):
        bundled = root / relative
        found = str(bundled) if bundled.is_file() else shutil.which(name)
        if not found:
            raise RuntimeError(f"缺少本地视频工具 {name}，请保留程序的完整文件夹。")
        result[name] = str(Path(found).resolve())
    return result
