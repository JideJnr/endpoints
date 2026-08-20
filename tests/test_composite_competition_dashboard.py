from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_competition_special_dashboard_route_imports_summary(monkeypatch):
    from app.competition import competition_special

    def fake_summary(buffer_limit: int = 200, analysis_limit: int = 1):
        return {
            "status": "success",
            "total_tracked": 0,
            "enabled_count": 0,
            "competitions": [],
            "errors": [],
            "buffer_limit": buffer_limit,
            "analysis_limit": analysis_limit,
        }

    monkeypatch.setattr(competition_special, "list_all_competition_summaries", fake_summary)

    response = TestClient(app).get(
        "/composite/competition-special/dashboard?buffer_limit=50&analysis_limit=2"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["buffer_limit"] == 50
    assert response.json()["analysis_limit"] == 2
