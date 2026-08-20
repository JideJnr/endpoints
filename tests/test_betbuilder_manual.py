from __future__ import annotations

import time
from datetime import date

from app.bet_builder import core


def test_upcoming_prediction_candidates_handles_filtered_empty(monkeypatch):
    """No surviving candidates should return an empty list, not raise while logging."""
    monkeypatch.setattr(core, "_research_filter_candidate", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        core,
        "_recent_ungraded_prediction_rows",
        lambda allowed_dates, limit: [
            {
                "match_id": "match-1",
                "match_name": "Home FC vs Away FC",
                "league_name": "Test League",
                "country_name": "Test Country",
                "best_pick": {
                    "type": "match_result",
                    "selection": "Home",
                    "confidence": 75,
                    "odds": 1.75,
                },
                "picks": [],
                "signals": [],
                "match_date": date.today().isoformat(),
                "start_time": time.time() + 3600,
                "is_live": False,
                "is_finished": False,
            }
        ],
    )

    assert core.upcoming_prediction_candidates(limit=1) == []
