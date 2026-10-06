"""Build the self-contained Windows desktop distribution using local tools."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess
import sys

import pefile
from version import APP_NAME, VERSION

ROOT = Path(__file__).resolve().parent


def write_version_info():
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo, StringFileInfo, StringStruct, StringTable,
        VarFileInfo, VarStruct, VSVersionInfo,
    )
    numbers = tuple(map(int, VERSION.split("."))) + (0,)
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3f,
                          flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("080404b0", [
            StringStruct("FileDescription", APP_NAME),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "clip-helper"),
            StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
            StringStruct("ProductName", APP_NAME),
            StringStruct("ProductVersion", VERSION),
        ])]), VarFileInfo([VarStruct("Translation", [2052, 1200])])],
    )
    target = ROOT / "work" / "version-info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(info), encoding="utf-8")
    return target


def copy_media_tools(gpac_directory: Path):
    destination = ROOT / "tools"
    destination.mkdir(exist_ok=True)
    ffmpeg = Path(shutil.which("ffmpeg") or "").resolve()
    ffprobe = Path(shutil.which("ffprobe") or "").resolve()
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise RuntimeError("FFmpeg / ffprobe are required to build")
    shutil.copy2(ffmpeg, destination / "ffmpeg.exe")
    shutil.copy2(ffprobe, destination / "ffprobe.exe")
    gpac_target = destination / "gpac"
    gpac_target.mkdir(exist_ok=True)
    files = {f.name.casefold(): f for f in gpac_directory.iterdir() if f.is_file()}
    pending = [gpac_directory / "mp4box.exe"]
    copied = set()
    while pending:
        source = pending.pop()
        if source.name.casefold() in copied:
            continue
        copied.add(source.name.casefold())
        shutil.copy2(source, gpac_target / source.name)
        binary = pefile.PE(str(source), fast_load=True)
        binary.parse_data_directories(directories=[1, 13])
        for attribute in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for imported in getattr(binary, attribute, []):
                name = imported.dll.decode().casefold()
                if name in files:
                    pending.append(files[name])
        binary.close()
    runtime = Path(sys.base_prefix) / "vcruntime140.dll"
    if runtime.exists():
        shutil.copy2(runtime, gpac_target / runtime.name)
    licenses = ROOT / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(gpac_directory / "License.txt", licenses / "GPAC-License.txt")
    shutil.copy2(ffmpeg.parent.parent / "LICENSE", licenses / "FFmpeg-License.txt")
    manifest = {"ffmpeg_source": str(ffmpeg), "ffprobe_source": str(ffprobe),
                "gpac_source": str(gpac_directory), "gpac_files": sorted(copied)}
    (ROOT / "work" / "build-tools.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("gpac_directory", nargs="?")
    parser.add_argument("--dist-dir", default=str(ROOT / "dist"))
    args = parser.parse_args()
    distribution = Path(args.dist_dir).resolve()
    if args.gpac_directory:
        copy_media_tools(Path(args.gpac_directory))
    version_info = write_version_info()
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
                    "--windowed", "--name", "片段助手", "--contents-directory", "_internal",
                    "--add-data", f"{ROOT / 'tools'};tools", "--add-data", f"{ROOT / 'licenses'};licenses",
                    "--add-data", f"{ROOT / 'assets'};assets", "--icon", str(ROOT / "assets/icon.ico"),
                    "--version-file", str(version_info),
                    "--distpath", str(distribution), "--workpath", "work/pyinstaller",
                    "--specpath", "work", str(ROOT / "main.py")], cwd=ROOT, check=True)
    shutil.copy2(ROOT / "使用说明.md", distribution / "片段助手" / "使用说明.md")
    for name in ("User Guide.md", "README.md", "README.en.md", "CHANGELOG.md"):
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, distribution / "片段助手" / name)
    # Qt imports Windows' unversioned ICU API. PATH can contain Poppler's
    # incompatible versioned ICU DLL, which PyInstaller mistakenly collects.
    # Keep Windows' system ICU and avoid shadowing it in the application.
    for name in ("icuuc.dll", "icudt78.dll"):
        (distribution / "片段助手" / "_internal" / name).unlink(missing_ok=True)
    # The desktop uses neither PDF images nor the optional on-screen keyboard.
    # Qt's generic GUI hook collects these GPL-only plugins transitively.
    qt = distribution / "片段助手" / "_internal" / "PySide6"
    for relative in ("Qt6VirtualKeyboard.dll", "plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
                     "Qt6Pdf.dll", "plugins/imageformats/qpdf.dll"):
        (qt / relative).unlink(missing_ok=True)
