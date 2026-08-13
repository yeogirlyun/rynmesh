from rynmesh.update_state import UpdateState


def test_defaults_and_roundtrip(tmp_path):
    s = UpdateState(tmp_path / "update-state.json")
    assert s.data["attempts"] == 0 and s.data["quarantined"] == [] and s.data["pending_version"] is None
    s.set(pending_version="0.3.0", pending_confirmed=False, attempts=1)
    s2 = UpdateState(tmp_path / "update-state.json")
    assert s2.data["pending_version"] == "0.3.0" and s2.data["attempts"] == 1


def test_quarantine(tmp_path):
    s = UpdateState(tmp_path / "update-state.json")
    s.quarantine("0.3.0"); s.quarantine("0.3.0")
    assert s.data["quarantined"] == ["0.3.0"]
    assert s.is_quarantined("0.3.0") and not s.is_quarantined("0.4.0")


def test_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "update-state.json"
    p.write_text("{not json")
    s = UpdateState(p)
    assert s.data["attempts"] == 0
