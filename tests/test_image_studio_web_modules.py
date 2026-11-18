from pathlib import Path
from urllib.parse import urlsplit
import re

from test_image_studio import call, studio


def test_page_module_graph_served_as_javascript(studio):
    _, _, site, _ = studio
    status, _, html = call(site, "GET", "/")
    assert status == 200
    entry = re.search(r'<script type="module" src="([^"]+)"', html.decode()).group(1)
    pending, loaded = [entry], set()
    while pending:
        path = urlsplit(pending.pop()).path
        if path in loaded:
            continue
        loaded.add(path)
        code, headers, source = call(site, "GET", path)
        assert code == 200 and "javascript" in headers["content-type"]
        module = source.decode()
        assert "innerHTML" not in module and "eval(" not in module
        for dependency in re.findall(r'from "(\./[^"]+)"', module):
            pending.append((Path(path).parent / dependency).as_posix())
    assert {"/form.js", "/detail.js", "/gallery.js"} <= loaded

