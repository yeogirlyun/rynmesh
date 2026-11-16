from rynmesh.recommendation_profile import RecommendationProfileStore, starter_items
from rynmesh.recommendation_service import recommend_from_items
from rynmesh.services.digest import _terms_of, parse_steering


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


def test_s13_i48_t1_chinese_direction_positive_negative_and_candidate_matching(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    store.patch({"direction": "多看 AI 和芯片，少看八卦；关注一些半导体相关的内容，屏蔽广告"})
    signals = store.signals()
    weights = signals["tag_weights"]
    assert weights["term:ai"] > 0
    assert weights["term:芯片"] > 0
    assert weights["term:八卦"] < 0
    assert weights["term:半导体"] > 0
    assert weights["term:广告"] < 0
    for noise in ("term:多看", "term:少看", "term:一些", "term:相关", "term:内容", "term:的"):
        assert noise not in weights

    items = [
        {
            "content_id": "cid_gossip",
            "title": "最新娱乐八卦汇总",
            "description": "每日热点闲聊",
            "tags": [],
            "content_kind": "document",
            "publisher_peer_id": "peer_1",
            "source_platform": "news",
            "safety_outcome": "passed",
            "fetch_status": "discovered",
            "published": "2026-11-16T00:00:00+00:00",
        },
        {
            "content_id": "cid_chip",
            "title": "新一代AI芯片架构解析",
            "description": "深入半导体设计细节",
            "tags": [],
            "content_kind": "document",
            "publisher_peer_id": "peer_1",
            "source_platform": "news",
            "safety_outcome": "passed",
            "fetch_status": "discovered",
            "published": "2026-11-16T00:00:00+00:00",
        },
    ]
    ranked = recommend_from_items(items, now_unix=1_800_000_000, limit=2, profile=signals)
    assert [r["contentId"] for r in ranked] == ["cid_chip", "cid_gossip"]
    assert "tag_match" in ranked[0]["evidence"]

    digest_terms = _terms_of({"title": "新一代AI芯片架构解析", "summary": "深入半导体设计细节"})
    assert "term:芯片" in digest_terms
    assert "term:半导体" in digest_terms
    steered = parse_steering("多看芯片，少看八卦")
    assert "term:芯片" in steered["interests"]
    assert "term:八卦" in steered["avoids"]


def test_s13_i48_t2_english_existing_cases_and_semicolon_resets_polarity(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    store.patch({"direction": "More local AI, less politics and no crypto"})
    legacy_weights = store.signals()["tag_weights"]
    assert legacy_weights["term:local"] > 0
    assert legacy_weights["term:ai"] > 0
    assert legacy_weights["term:politics"] < 0
    assert legacy_weights["term:crypto"] < 0

    store.patch({"direction": "no crypto; ai agents"})
    weights = store.signals()["tag_weights"]
    assert weights["term:crypto"] < 0
    assert weights["term:ai"] > 0
    assert weights["term:agents"] > 0


def test_s13_i48_t3_comma_list_after_negative_keeps_negative_within_sentence(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    store.patch({"direction": "avoid crypto, nft"})
    weights = store.signals()["tag_weights"]
    assert weights["term:crypto"] < 0
    assert weights["term:nft"] < 0

    store.patch({"direction": "不要八卦、绯闻"})
    cn_weights = store.signals()["tag_weights"]
    assert cn_weights["term:八卦"] < 0
    assert cn_weights["term:绯闻"] < 0


def test_s13_i48_t4_chinese_adversative_clause_resets_and_flips_to_positive(tmp_path):
    store = RecommendationProfileStore(tmp_path / "profile.json")
    store.patch({"direction": "不要八卦，但是多看芯片"})
    weights = store.signals()["tag_weights"]
    assert weights["term:八卦"] < 0
    assert weights["term:芯片"] > 0

    store.patch({"direction": "不看短视频，不过关注开源软件"})
    weights2 = store.signals()["tag_weights"]
    assert weights2["term:短视频"] < 0
    assert weights2["term:开源软件"] > 0
