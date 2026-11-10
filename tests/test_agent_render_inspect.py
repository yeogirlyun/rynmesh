"""Visual observation for text-only models: an isolated render measured into text evidence."""
import hashlib

import pytest
from PIL import Image, ImageDraw
from test_agent_local_tools import local as local  # noqa: F401 - shared workspace fixture

from rynmesh.agent_runtime.local_render import attachments, classify, color_grid, loop_period
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.store import AgentToolRejected

# A wheel and a crank turn once per 2 s. One leg's foot follows the crank's pedal through eight
# keyframes; the other slides left and right, so its contact with the pedal breaks. A foot group
# was moved far outside the drawing, a script tries to paint the sky red, and an image points
# at the network.
PEDAL = [(round(120 + 20 * c, 1), round(60 + 20 * s, 1)) for c, s in
         [(1, 0), (0.7071, 0.7071), (0, 1), (-0.7071, 0.7071), (-1, 0), (-0.7071, -0.7071), (0, -1), (0.7071, -0.7071), (1, 0)]]
FOLLOW = ";".join(f"M120 20 L{x} {y}" for x, y in PEDAL)
SLIDE = ";".join(f"M110 20 L{x} 60" for x in (140, 100, 140, 100, 140, 100, 140, 100, 140))
KEYS = ";".join(str(k / 8) for k in range(9))
SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100" width="400" height="200">
  <rect id="sky" width="200" height="100" fill="#eef6ff"/>
  <script>document.getElementById('sky').setAttribute('fill', '#ff0000')</script>
  <image href="https://example.com/cloud.png" x="0" y="0" width="10" height="10"/>
  <!-- rear wheel -->
  <g id="wheel"><circle cx="50" cy="60" r="25" fill="none" stroke="#111" stroke-width="4"/>
    <line x1="50" y1="35" x2="50" y2="85" stroke="#111" stroke-width="3"/>
    <animateTransform attributeName="transform" type="rotate" from="0 50 60" to="360 50 60" dur="2s" repeatCount="indefinite"/></g>
  <!-- crank -->
  <g id="crank"><line x1="120" y1="60" x2="140" y2="60" stroke="#333" stroke-width="3"/>
    <!-- pedal -->
    <rect id="pedal" x="136" y="58" width="8" height="4" fill="#333"/>
    <animateTransform attributeName="transform" type="rotate" from="0 120 60" to="360 120 60" dur="2s" repeatCount="indefinite"/></g>
  <!-- good leg -->
  <path id="leg" d="M120 20 L140 60" stroke="#e67e22" stroke-width="3" fill="none">
    <animate attributeName="d" values="{FOLLOW}" keyTimes="{KEYS}" dur="2s" repeatCount="indefinite"/></path>
  <!-- sliding leg -->
  <path id="slider" d="M110 20 L140 60" stroke="#2e86de" stroke-width="3" fill="none">
    <animate attributeName="d" values="{SLIDE}" keyTimes="{KEYS}" dur="2s" repeatCount="indefinite"/></path>
  <!-- lost foot -->
  <g id="foot" transform="translate(-500 0)"><ellipse cx="60" cy="90" rx="8" ry="4" fill="#e67e22"/></g>
</svg>"""


def test_loop_period_is_the_common_multiple_of_animation_durations():
    assert loop_period({"smil": [{"dur": "1.5s"}, {"dur": "750ms"}], "css": []}) == 1.5
    assert loop_period({"smil": [{"dur": "2s"}], "css": [{"duration_ms": 3000}]}) == 6
    assert loop_period({"smil": [{"dur": "indefinite"}], "css": []}) is None


def test_color_map_keeps_thin_dark_strokes_visible_on_a_light_background():
    image = Image.new("RGB", (400, 200), "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.ellipse((150, 50, 250, 150), outline="#000000", width=5)
    draw.rectangle((20, 20, 60, 60), fill="#ff8800")
    grid, legend = color_grid(image)
    assert legend["."] == "most common color: white"
    assert legend["K"] == "black/very dark" and legend["O"] == "orange"
    middle = grid[len(grid) // 2]
    assert middle.count("K") >= 2 and middle.strip(".").startswith("K")
    assert classify(255, 0, 0) == "R" and classify(191, 227, 242) == "c" and classify(120, 120, 120) == "g"


def test_contacts_are_reported_only_for_moving_joints_and_say_when_they_break():
    elements = [{"label": "path#leg"}, {"label": "rect#pedal"}, {"label": "path#tube"}]
    frames = [[{"ends": [[0, 0], [10, 10]]}, {"box": [8, 8, 4, 4]}, {"ends": [[0, 0], [5, 5]]}],
              [{"ends": [[0, 0], [30, 10]]}, {"box": [8, 8, 4, 4]}, {"ends": [[0, 0], [5, 5]]}]]
    found = attachments(elements, frames, 100 * 100)
    assert found == ["path#leg end -> rect#pedal: distance per frame 0,18 (contact breaks)"]


def test_a_part_inside_a_diagonal_rods_box_but_away_from_its_end_is_not_attached():
    # A foot that drifts along the inside of a shin's bounding box once looked attached (distance 0).
    elements = [{"label": "line(#d18f2a)"}, {"label": "path(#d18f2a)"}]
    frames = [[{"ends": [[0, 0], [40, 40]], "box": [0, 0, 40, 40]}, {"box": [36, 36, 8, 6]}],
              [{"ends": [[0, 0], [44, 36]], "box": [0, 0, 44, 36]}, {"box": [10, 25, 8, 6]}]]
    assert attachments(elements, frames, 200 * 200) == [
        "line(#d18f2a) end -> path(#d18f2a): distance per frame 0,26.5 (contact breaks)",
        "path(#d18f2a) -> line(#d18f2a) end: distance per frame 0,26.5 (contact breaks)"]


def test_render_inspect_is_a_read_tool_that_needs_no_desktop_permission():
    description, effect, _risk, schema = SPECS["ryn_render_inspect"]
    assert effect == "read" and schema["required"] == ["path"]
    assert "isolated headless browser" in description and "writes no files" in description


def test_render_inspect_rejects_paths_outside_the_workspace_and_unsupported_files(local):  # noqa: F811
    tools, context, workspace, _ = local
    (workspace / "notes.txt").write_text("plain", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_local_path_outside_workspace"):
        tools.perform("ryn_render_inspect", {"path": "../escape.svg"}, context, lambda: False)
    with pytest.raises(AgentToolRejected, match="agent_render_unsupported_type"):
        tools.perform("ryn_render_inspect", {"path": "notes.txt"}, context, lambda: False)


def test_real_render_measures_rotation_contacts_offscreen_parts_and_isolation(local):  # noqa: F811
    from rynmesh.agent_runtime.local_browser import find_browser
    if not find_browser():
        pytest.skip("Chromium is not installed")
    tools, context, workspace, _ = local
    (workspace / "bike.svg").write_bytes(SVG.encode())
    before = sorted(path.name for path in workspace.iterdir())
    result = tools.perform("ryn_render_inspect", {"path": "bike.svg"}, context, lambda: False)

    assert result["sha256"] == hashlib.sha256(SVG.encode()).hexdigest()
    assert sorted(path.name for path in workspace.iterdir()) == before, "inspection must not write files"
    assert result["animation"]["loop_period_seconds"] == 2
    assert result["animation"]["sampled_seconds"] == [0, 0.333, 0.667, 1.0, 1.333, 1.667]
    assert result["animation"]["loop_seam"]["seamless"] is True
    wheel = next(item for item in result["elements"] if item["element"].startswith("g#wheel"))
    assert [float(line.split("rot ")[1]) for line in wheel["by_frame"][:3]] == pytest.approx([0, 60, 120], abs=0.5)
    assert any("path#leg" in line and line.endswith("(contact holds)") and "rect#pedal" in line for line in result["attachments"])
    assert any("path#slider" in line and line.endswith("(contact breaks)") for line in result["attachments"])
    assert any("g#foot" in finding and "outside the visible area in 6 of 6" in finding for finding in result["findings"])
    # Isolation: the script did not run (no red sky) and the network image was blocked and reported.
    assert result["render"]["scripts_executed"] is False and result["render"]["scripts_present"] >= 1
    assert "R" not in result["color_legend"] and "r" not in result["color_legend"]
    assert result["render"]["external_references"] == ["https://example.com/cloud.png"]
    assert result["render"]["blocked_requests"] == []  # the CSP stopped it before any request left
    assert any(finding.startswith("Not self-contained") for finding in result["findings"])
    assert result["render"]["view_box"] == [0, 0, 200, 100] and result["render"]["user_units_per_px"] == 0.5


def test_real_render_reports_a_loop_that_jumps_back(local):  # noqa: F811
    from rynmesh.agent_runtime.local_browser import find_browser
    if not find_browser():
        pytest.skip("Chromium is not installed")
    tools, context, workspace, _ = local
    (workspace / "jump.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100"><rect width="200" height="100" fill="#fff"/>'
        '<rect id="box" x="0" y="30" width="40" height="40" fill="#123">'
        '<animate attributeName="x" values="0;160" dur="1s" repeatCount="indefinite"/></rect></svg>', encoding="utf-8")
    result = tools.perform("ryn_render_inspect", {"path": "jump.svg", "times": [0, 0.5]}, context, lambda: False)
    assert result["animation"]["sampled_seconds"] == [0, 0.5]
    assert result["animation"]["loop_seam"]["seamless"] is False
    assert any(finding.startswith("Loop seam") for finding in result["findings"])
