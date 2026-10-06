# Clip Helper

**v1.1.0 · Windows 64-bit desktop app**

Manually mark one or more parts of a video to keep, preview the result, then export a new file or confirm replacing the source. Videos are processed locally and are never uploaded.

[简体中文](README.md) · [Full user guide](User%20Guide.md) · [使用说明](使用说明.md)

## Download and launch

Download `clip-helper-v1.1.0-windows-x64.zip` from [GitHub Releases](https://github.com/kylefu8/clip-helper/releases), extract the entire archive, and run `片段助手.exe` (the executable keeps its Chinese filename). Keep the `_internal` folder beside it. Python installation is not required. Windows 10/11 64-bit is recommended.

## Interface language

Use the in-app language selector to switch instantly between Simplified Chinese and English. The choice is remembered. On a fresh installation, the initial language follows the system language: Chinese on Chinese-language systems and English otherwise. Switching languages keeps the current video and marked segments loaded, and does not change file names or where marker data is stored. The selector is unavailable while processing is in progress.

## Quick start

1. Choose a video folder, then select a video from the list.
2. Play or scrub the timeline. Press **I** to mark a start and **O** to mark an end.
3. Click the control that adds the marked range to the keep list. Each range must be added; one video can contain multiple retained segments.
4. Prepare the marked videos and wait for processing and validation to finish.
5. Review the result, then export a new file or confirm replacing the source.

The app does not re-encode the video. Each segment start is aligned backward to a keyframe, so a little extra footage may remain at the beginning. Multiple segments are joined in time order, and the actual retained ranges are shown in the result.

**Confirming replacement permanently removes all footage outside the retained segments. No source copy remains after a successful replacement.** Choose export if you need to keep the original.

See the [English user guide](User%20Guide.md) for more details. Build instructions are in the developer section below. Third-party notices and licenses are in [licenses/NOTICE.txt](licenses/NOTICE.txt).

## Build from source

Install the Python dependencies listed in `requirements.txt` and provide FFmpeg, ffprobe, and GPAC MP4Box in `PATH` or under the project `tools` directory. Then run:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe build.py
```

The shared app and release version is defined in `version.py`. The source repository contains no user videos, marker data, caches, or runtime records.