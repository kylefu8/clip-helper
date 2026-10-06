# Clip Helper v1.1.0 — User Guide

Run `片段助手.exe` to open the standalone Windows app. Keep the `_internal` folder beside the executable. Python is not required, and your videos are never uploaded.

## Change the interface language

Use the language selector to switch immediately between Simplified Chinese and English. The app remembers your choice. On a fresh installation, the initial language follows the system language: Chinese on Chinese-language systems, English otherwise. Switching keeps the current video and marked segments loaded; it does not rename files or change where marker data is stored. Language switching is disabled while processing is in progress.

## Trim videos

1. Choose a video folder. Its videos load automatically. **Refresh** scans it again and preserves marks for files that still exist and have not changed. The app briefly checks each source on first read; you can watch while it checks, then mark ranges when the check is complete.
2. Play or scrub to the range you want to keep. Press **I** to mark its start and **O** to mark its end.
3. Click the control that adds the range to the retained-segment list. Marking the start and end alone does not add a segment. A video can have multiple segments; you can review, edit, or remove them.
4. Prepare the marked videos and wait for processing and validation to finish. Multiple ranges are joined in source time order, and overlapping ranges are merged.
5. Review the result, then export a new file or confirm replacing the source.

Marks are saved automatically. The last folder and its marks are restored when you reopen the app. If a source video changes, the app stops reusing its old marks.

Select one or more videos and use **Remove selected**, right-click to remove them, or press Delete. This only hides the list entries; it does not delete videos or marks. Hidden entries stay hidden after refresh and restart. Use **Restore removed items** to show them and their marks again.

The first frame appears after a video loads. Scrubbing updates the picture as you drag, then returns to the previous play or pause state. The **Previous frame / Next frame** controls pause playback and show adjacent frames. Press **Space** to play or pause the selected video, including when focus is on the list, timeline, or an action button. In a text field, Space still types a space.

## Lossless trim boundaries

The app copies the existing compressed video without re-encoding it. Each segment start is aligned backward to a keyframe that can be decoded independently, so a little extra footage may be kept at the start. The result view shows the actual retained ranges. Review each beginning and each join in the preview.

DJI MP4 files retain their video, audio, camera-data tracks, orientation, and original metadata. The app validates the prepared result before allowing replacement. It reports an error if a file cannot be fully preserved or validated.

**Confirming replacement permanently removes footage outside the retained segments. No source copy remains after success.** Export a new file when you need to keep the original.

Markers and runtime records are stored in `%LOCALAPPDATA%\片段助手`. Before confirmation, prepared results are kept temporarily in the `.片段助手缓存` folder beside the source video, so make sure there is enough free space. Temporary large media is removed after successful processing.

## Development and verification

Dependency versions are listed in `requirements.txt`. Replacement tests use synthetic media only.

With the project virtual environment, run `python -m unittest discover -s tests -v`. Build with `python build.py` after providing FFmpeg, ffprobe, and GPAC MP4Box. The shared version is defined in `version.py`.

Third-party licenses are in the app's `_internal\licenses` folder.