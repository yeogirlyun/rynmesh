from rynmesh.recommendation_profile import RecommendationProfileStore, starter_items


def test_profile_defaults_include_choices(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    profile = store.public()
    assert profile["direction"] == ""
    assert profile["topic_choices"]
    assert profile["platform_choices"]


def test_profile_patch_filters_unknown_choices(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    profile = store.patch(
        {
            "direction": "Local AI and open source",
            "topics": ["ai-agents", "unknown"],
            "platforms": ["github", "made-up"],
        }
    )
    assert profile["topics"] == ["ai-agents"]
    assert profile["platforms"] == ["github"]
    signals = store.signals()
    assert signals["tag_weights"]["ai"] > 0
    assert signals["tag_weights"]["platform:github"] > 0
    assert signals["tag_weights"]["term:local"] > 0


def test_profile_direction_understands_positive_and_negative_clauses(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    store.patch({"direction": "More local AI, less politics and no crypto"})
    signals = store.signals()
    assert signals["tag_weights"]["term:local"] > 0
    assert signals["tag_weights"]["term:ai"] > 0
    assert signals["tag_weights"]["term:politics"] < 0
    assert signals["tag_weights"]["term:crypto"] < 0


def test_feedback_rebuilds_positive_negative_and_hidden_signals(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    item = {
        "content_id": "cid_one",
        "tags": ["agents", "platform:github"],
        "publisher_peer_id": "peer_one",
        "source_platform": "github",
    }
    store.feedback(item, "more")
    signals = store.signals()
    assert signals["tag_weights"]["agents"] > 0
    assert signals["publisher_weights"]["peer_one"] > 0
    store.feedback(item, "hide")
    signals = store.signals()
    assert "cid_one" in signals["hidden_content_ids"]
    assert signals["tag_weights"]["agents"] < 0


def test_starter_items_are_stable_for_a_node_and_day():
    first = starter_items({}, seed_key="peer-a", now_unix=1_800_000_000)
    second = starter_items({}, seed_key="peer-a", now_unix=1_800_000_000)
    assert [item["content_id"] for item in first] == [item["content_id"] for item in second]
    assert all(item["starter"] for item in first)
