from clip_agent.search import reciprocal_rank_fusion


def test_rrf_rewards_items_present_in_both_rankings():
    ranking = reciprocal_rank_fusion([["a", "b"], ["b", "c"]])
    assert ranking[0][0] == "b"
