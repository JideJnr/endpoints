import unittest

from app.buffer import _is_sporty_match_active, _reusable_sofascore_detail


class SofaScoreReuseTests(unittest.TestCase):
    def test_reuses_matching_prematch_detail(self):
        detail = {"id": 101, "standings": [{"position": 1}]}
        result = _reusable_sofascore_detail(
            {"sofascore_detail": detail}, {"id": 101}, is_live=False
        )
        self.assertIs(result, detail)

    def test_does_not_reuse_live_or_wrong_event_detail(self):
        existing = {"sofascore_detail": {"id": 101}}
        self.assertIsNone(_reusable_sofascore_detail(existing, {"id": 101}, is_live=True))
        self.assertIsNone(_reusable_sofascore_detail(existing, {"id": 202}, is_live=False))

    def test_prematch_is_active_even_if_provider_returned_it_in_live_scope(self):
        self.assertTrue(_is_sporty_match_active({"state": "prematch", "is_prematch": True, "is_live": False}))
        self.assertTrue(_is_sporty_match_active({"state": "live", "is_prematch": False, "is_live": True}))
        self.assertFalse(_is_sporty_match_active({"state": "finished", "is_finished": True}))


if __name__ == "__main__":
    unittest.main()
