from __future__ import annotations

import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from media_engine import UnsupportedMediaError, _probe, _track_config_check


def response(profile=None):
    audio = {"index": 1, "codec_type": "audio", "codec_name": "aac"}
    if profile is not None:
        audio["profile"] = profile
    return subprocess.CompletedProcess([], 0, json.dumps({"streams": [audio]}), "")


class ProbeTests(unittest.TestCase):
    @patch("media_engine._run_capture")
    def test_missing_aac_profile_is_read_before_configuration_check(self, run):
        run.side_effect = [response(), response("LC")]
        cancel = threading.Event()
        probe = _probe(Path("clip.mp4"), "ffprobe", cancel)
        self.assertEqual(probe["streams"][0]["profile"], "LC")
        self.assertEqual(run.call_count, 2)
        retry = run.call_args_list[1]
        self.assertIn("-probesize", retry.args[0])
        self.assertIn("-analyzeduration", retry.args[0])
        self.assertIs(retry.kwargs["cancel_event"], cancel)

    @patch("media_engine._run_capture", return_value=response("LC"))
    def test_complete_profile_uses_default_probe(self, run):
        _probe(Path("clip.mp4"), "ffprobe")
        self.assertEqual(run.call_count, 1)

    @patch("media_engine._run_capture")
    def test_unidentified_profile_is_not_accepted(self, run):
        run.side_effect = [response(), response("unknown")]
        with self.assertRaisesRegex(UnsupportedMediaError, "AAC"):
            _probe(Path("clip.mp4"), "ffprobe")
        self.assertEqual(run.call_count, 2)

    def test_real_profile_difference_is_still_rejected(self):
        source = json.loads(response("LC").stdout)["streams"][0]
        candidate = json.loads(response("HE-AAC").stdout)["streams"][0]
        self.assertEqual(_track_config_check(source, candidate), ["profile"])


if __name__ == "__main__":
    unittest.main()
