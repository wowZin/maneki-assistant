"""tests for pipelines/maneki/maneki_pipe.py"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipelines.maneki.maneki_pipe import write_inbox


def test_write_inbox_appends_jsonl(tmp_path):
    with patch("pipelines.maneki.maneki_pipe.INBOX_DIR", tmp_path):
        write_inbox("oc_123", {"text": "hello", "message_id": "m1"})
        write_inbox("oc_123", {"text": "world", "message_id": "m2"})

        inbox_file = tmp_path / "oc_123.jsonl"
        assert inbox_file.exists()
        lines = inbox_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["text"] == "hello"
        assert json.loads(lines[1])["text"] == "world"


def test_write_inbox_creates_inbox_dir(tmp_path):
    nested = tmp_path / "inbox"
    with patch("pipelines.maneki.maneki_pipe.INBOX_DIR", nested):
        write_inbox("oc_456", {"text": "hi"})
        assert (nested / "oc_456.jsonl").exists()
