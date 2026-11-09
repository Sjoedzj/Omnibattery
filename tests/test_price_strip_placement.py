"""Pin the shipped manifest version.

This file used to hold the placement tests of our own price strip under the day
chart. That strip is gone: FFunes' ``release/v1.6.0`` already draws the quarter
price as a stepped line inside the chart band, with its own high/low scale, so a
second band below it showed the same price twice. Its placement tests, the Node
harness that drove them and the stage/viewport height assertions all went with
it.

Only the release pin that this work brought along is left: the version has to be
bumped by hand for every release and the manifest must stay BOM-free.
"""
from __future__ import annotations

import json
from pathlib import Path

from custom_components import omnibattery

COMPONENT = Path(omnibattery.__file__).parent
MANIFEST = COMPONENT / "manifest.json"
EXPECTED_VERSION = "1.6.0b9"


def test_manifest_version_is_bumped_and_has_no_bom():
    raw = MANIFEST.read_bytes()
    assert raw[:3] == b"{\n "
    assert raw[:3] != b"\xef\xbb\xbf"
    assert json.loads(raw.decode("utf-8"))["version"] == EXPECTED_VERSION
