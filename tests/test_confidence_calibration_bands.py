"""
Unit tests for calibration band separation in confidence_calibrator.py (R18).

Covers:
  - rebuild_calibration() with predictions at confidence 85 and 95 produces
    separate rows for band_low=80 and band_low=90.
  - calibrate_confidence(pick_type, 85) resolves to band_low=80.
  - calibrate_confidence(pick_type, 92) resolves to band_low=90.
  - All existing confidence bands (50-79) still resolve correctly.
  - Confidence exactly at boundary values (80, 90) map to the right band.

Requirements: R18.1, R18.2, R18.3, R18.5
"""
from __future__ import annotations

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# In-memory DB fixture helpers
# ---------------------------------------------------------------------------

def _make_prediction_history_db() -> sqlite3.Connection:
    """Create an in-memory DB with both prediction history tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Minimal schema — only the columns referenced by UNIQUE_GRADED_HISTORY
    for table in ("prediction_history", "prediction_candidate_history"):
        conn.execute(f"""
            create table {table} (
                id integer primary key autoincrement,
                match_id text,
                pick_type text,
                selection text,
                confidence integer,
                result text,
                created_at text default current_timestamp,
                graded_at text
            )
        """)
    return conn


def _insert_prediction(
    conn: sqlite3.Connection,
    pick_type: str,
    confidence: int,
    result: str,
    match_id: str | None = None,
    table: str = "prediction_history",
) -> None:
    if match_id is None:
        # Use a unique match_id based on rowid to avoid dedup collisions
        match_id = f"match_{pick_type}_{confidence}_{result}_{id(object())}"
    conn.execute(
        f"insert into {table} "
        "(match_id, pick_type, selection, confidence, result, graded_at) "
        "values (?, ?, ?, ?, ?, datetime('now'))",
        (match_id, pick_type, "home", confidence, result),
    )


def _patch_calibrator(conn: sqlite3.Connection, monkeypatch):
    """
    Redirect confidence_calibrator's DB connection to use our in-memory DB.
    Both _conn() (used by calibrate_confidence/rebuild_calibration) and
    _init_db() are patched to no-ops / our connection.
    """
    from app.enrichment import confidence_calibrator as cc

    @contextmanager
    def _fake_conn():
        yield conn

    monkeypatch.setattr(cc, "_conn", _fake_conn)
    monkeypatch.setattr(cc, "_init_db", lambda: None)


# ---------------------------------------------------------------------------
# Tests: rebuild_calibration() produces separate 80 and 90 band rows
# ---------------------------------------------------------------------------

class TestRebuildCalibrationBandSeparation:
    """R18.1, R18.2, R18.3: 80-89 and 90-99 must produce distinct band_low rows."""

    def test_confidence_85_produces_band_low_80(self, monkeypatch):
        """A prediction with confidence=85 must land in band_low=80, not 90."""
        db = _make_prediction_history_db()
        _insert_prediction(db, "match_result", 85, "win", match_id="m85")
        _patch_calibrator(db, monkeypatch)

        from app.enrichment.confidence_calibrator import rebuild_calibration
        rebuild_calibration()

        row = db.execute(
            "select band_low from confidence_calibration "
            "where pick_type='match_result' and band_low=80"
        ).fetchone()
        assert row is not None, "band_low=80 row must exist for confidence=85 prediction"

    def test_confidence_95_produces_band_low_90(self, monkeypatch):
        """A prediction with confidence=95 must land in band_low=90, not 80."""
        db = _make_prediction_history_db()
        _insert_prediction(db, "match_result", 95, "win", match_id="m95")
        _patch_calibrator(db, monkeypatch)

        from app.enrichment.confidence_calibrator import rebuild_calibration
        rebuild_calibration()

        row = db.execute(
            "select band_low from confidence_calibration "
            "where pick_type='match_result' and band_low=90"
        ).fetchone()
        assert row is not None, "band_low=90 row must exist for confidence=95 prediction"

    def test_confidence_85_and_95_produce_two_distinct_rows(self, monkeypatch):
        """Both confidences in the same pick_type must create separate band rows."""
        db = _make_prediction_history_db()
        _insert_prediction(db, "match_result", 85, "win", match_id="m85a")
        _insert_prediction(db, "match_result", 95, "win", match_id="m95a")
        _patch_calibrator(db, monkeypatch)

        from app.enrichment.confidence_calibrator import rebuild_calibration
        rebuild_calibration()

        rows = db.execute(
            "select band_low from confidence_calibration "
            "where pick_type='match_result' and band_low in (80, 90) "
            "order by band_low"
        ).fetchall()
        band_lows = [r["band_low"] for r in rows]
        assert 80 in band_lows, "band_low=80 must be present"
        assert 90 in band_lows, "band_low=90 must be present"
        assert len(band_lows) == 2, f"Expected exactly 2 distinct bands, got: {band_lows}"

    def test_old_single_80plus_band_no_longer_merges(self, monkeypatch):
        """Predictions at 80 and 90 must NOT be merged into a single band_low=80 row."""
        db = _make_prediction_history_db()
        # 3 rows at confidence=82 and 3 rows at confidence=92
        for i in range(3):
            _insert_prediction(db, "match_result", 82, "win", match_id=f"m82_{i}")
        for i in range(3):
            _insert_prediction(db, "match_result", 92, "win", match_id=f"m92_{i}")
        _patch_calibrator(db, monkeypatch)

        from app.enrichment.confidence_calibrator import rebuild_calibration
        rebuild_calibration()

        # Verify they are separate rows, not merged
        row_80 = db.execute(
            "select samples from confidence_calibration "
            "where pick_type='match_result' and band_low=80"
        ).fetchone()
        row_90 = db.execute(
            "select samples from confidence_calibration "
            "where pick_type='match_result' and band_low=90"
        ).fetchone()
        assert row_80 is not None, "band_low=80 row must exist"
        assert row_90 is not None, "band_low=90 row must exist"
        assert row_80["samples"] == 3, f"band_low=80 must have 3 samples, got {row_80['samples']}"
        assert row_90["samples"] == 3, f"band_low=90 must have 3 samples, got {row_90['samples']}"

    def test_global_band_also_separated(self, monkeypatch):
        """The __global__ pick_type also produces separate 80 and 90 band rows."""
        db = _make_prediction_history_db()
        _insert_prediction(db, "match_result", 85, "win", match_id="gm85")
        _insert_prediction(db, "match_result", 95, "win", match_id="gm95")
        _patch_calibrator(db, monkeypatch)

        from app.enrichment.confidence_calibrator import rebuild_calibration
        rebuild_calibration()

        rows = db.execute(
            "select band_low from confidence_calibration "
            "where pick_type='__global__' and band_low in (80, 90)"
        ).fetchall()
        band_lows = {r["band_low"] for r in rows}
        assert 80 in band_lows, "__global__ must have band_low=80"
        assert 90 in band_lows, "__global__ must have band_low=90"


# ---------------------------------------------------------------------------
# Tests: calibrate_confidence() resolves to the correct band
# ---------------------------------------------------------------------------

class TestCalibrateConfidenceBandResolution:
    """R18.5: calibrate_confidence() must query the correct band_low for each input."""

    def _setup_bands(
        self, db: sqlite3.Connection, pick_type: str, bands: dict[int, tuple[int, int]]
    ) -> None:
        """
        Insert pre-built calibration rows for the given bands.
        bands is {band_low: (wins, losses)}.
        """
        from app.enrichment.confidence_calibrator import _init_calibration_table, MIN_SAMPLES
        _init_calibration_table(db)
        for band_low, (wins, losses) in bands.items():
            samples = wins + losses
            win_rate = round(wins / samples, 4) if samples > 0 else None
            db.execute(
                "insert or replace into confidence_calibration "
                "(pick_type, band_low, samples, wins, losses, win_rate) "
                "values (?, ?, ?, ?, ?, ?)",
                (pick_type, band_low, samples, wins, losses, win_rate),
            )

    def test_confidence_85_uses_band_80(self, monkeypatch):
        """calibrate_confidence(pick_type, 85) must look up band_low=80."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)

        # 30 samples to meet MIN_SAMPLES: 20 wins, 10 losses → win_rate ≈ 0.667
        self._setup_bands(db, "match_result", {
            80: (20, 10),
        })

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 85)

        assert result["calibrated"] is True, (
            "Should find the band_low=80 row for confidence=85"
        )
        # win_rate for band_low=80 is 20/(20+10) ≈ 0.667
        assert result["win_rate"] is not None
        assert abs(result["win_rate"] - 66.7) < 0.2, (
            f"Expected win_rate ≈ 66.7 from band_low=80, got {result['win_rate']}"
        )

    def test_confidence_92_uses_band_90(self, monkeypatch):
        """calibrate_confidence(pick_type, 92) must look up band_low=90."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)

        # 30 samples to meet MIN_SAMPLES: 25 wins, 5 losses → win_rate ≈ 0.833
        self._setup_bands(db, "match_result", {
            90: (25, 5),
        })

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 92)

        assert result["calibrated"] is True, (
            "Should find the band_low=90 row for confidence=92"
        )
        assert result["win_rate"] is not None
        assert abs(result["win_rate"] - 83.3) < 0.2, (
            f"Expected win_rate ≈ 83.3 from band_low=90, got {result['win_rate']}"
        )

    def test_confidence_85_does_not_use_band_90(self, monkeypatch):
        """confidence=85 must NOT be calibrated by the band_low=90 row."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)

        # Only band_low=90 has data; band_low=80 is absent
        self._setup_bands(db, "match_result", {
            90: (30, 5),  # win_rate = 0.857
        })

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 85)

        # band_low=80 has no data → should fall through to uncalibrated
        assert result["calibrated"] is False, (
            "confidence=85 must not use band_low=90 data — it belongs to band_low=80"
        )

    def test_confidence_92_does_not_use_band_80(self, monkeypatch):
        """confidence=92 must NOT be calibrated by the band_low=80 row."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)

        # Only band_low=80 has data; band_low=90 is absent
        self._setup_bands(db, "match_result", {
            80: (30, 10),  # win_rate = 0.75
        })

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 92)

        # band_low=90 has no data → falls through to uncalibrated
        assert result["calibrated"] is False, (
            "confidence=92 must not use band_low=80 data — it belongs to band_low=90"
        )

    def test_confidence_80_maps_to_band_80(self, monkeypatch):
        """Boundary: confidence exactly 80 → band_low=80."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)
        self._setup_bands(db, "match_result", {80: (20, 10)})

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 80)

        assert result["calibrated"] is True
        assert abs(result["win_rate"] - 66.7) < 0.2

    def test_confidence_90_maps_to_band_90(self, monkeypatch):
        """Boundary: confidence exactly 90 → band_low=90."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)
        # Use 35 samples to exceed MIN_SAMPLES (30)
        self._setup_bands(db, "match_result", {90: (28, 7)})

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 90)

        assert result["calibrated"] is True
        assert abs(result["win_rate"] - 80.0) < 0.2

    def test_confidence_89_maps_to_band_80(self, monkeypatch):
        """Boundary: confidence=89 is in the 80-89 range → band_low=80."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)
        # Use 30 samples to meet MIN_SAMPLES
        self._setup_bands(db, "match_result", {80: (20, 10)})

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 89)

        assert result["calibrated"] is True

    def test_confidence_99_maps_to_band_90(self, monkeypatch):
        """Boundary: confidence=99 is in the 90-99 range → band_low=90."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)
        # Use 35 samples to exceed MIN_SAMPLES (30)
        self._setup_bands(db, "match_result", {90: (28, 7)})

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 99)

        assert result["calibrated"] is True


# ---------------------------------------------------------------------------
# Tests: lower bands still resolve correctly (regression guard)
# ---------------------------------------------------------------------------

class TestLowerBandsUnchanged:
    """Confidence values below 80 must still resolve to the same bands as before."""

    def _setup_single_band(self, db: sqlite3.Connection, pick_type: str, band_low: int) -> None:
        from app.enrichment.confidence_calibrator import _init_calibration_table
        _init_calibration_table(db)
        db.execute(
            "insert or replace into confidence_calibration "
            "(pick_type, band_low, samples, wins, losses, win_rate) values (?,?,?,?,?,?)",
            (pick_type, band_low, 40, 22, 18, round(22 / 40, 4)),
        )

    @pytest.mark.parametrize("confidence,expected_band", [
        (50, 50), (55, 50), (59, 50),
        (60, 60), (65, 60), (69, 60),
        (70, 70), (75, 70), (79, 70),
    ])
    def test_below_80_bands_unchanged(self, confidence, expected_band, monkeypatch):
        """Confidence values < 80 still resolve to their 10-point band."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)
        self._setup_single_band(db, "match_result", expected_band)

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", confidence)

        assert result["calibrated"] is True, (
            f"confidence={confidence} should resolve to band_low={expected_band}"
        )


# ---------------------------------------------------------------------------
# Tests: band label in calibrate_confidence() return value
# ---------------------------------------------------------------------------

class TestBandLabel:
    """The 'band' label in the return dict must correctly describe the band range."""

    def _setup_band(self, db: sqlite3.Connection, pick_type: str, band_low: int) -> None:
        from app.enrichment.confidence_calibrator import _init_calibration_table
        _init_calibration_table(db)
        db.execute(
            "insert or replace into confidence_calibration "
            "(pick_type, band_low, samples, wins, losses, win_rate) values (?,?,?,?,?,?)",
            (pick_type, band_low, 40, 22, 18, round(22 / 40, 4)),
        )

    def test_band_label_for_confidence_85_is_80_89(self, monkeypatch):
        """confidence=85 → band label must be '80-89%', not '80%+'."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)
        self._setup_band(db, "match_result", 80)

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 85)

        assert result.get("band") == "80-89%", (
            f"Expected band='80-89%', got '{result.get('band')}'"
        )

    def test_band_label_for_confidence_95_is_90_99(self, monkeypatch):
        """confidence=95 → band label must be '90-99%'."""
        db = _make_prediction_history_db()
        _patch_calibrator(db, monkeypatch)
        self._setup_band(db, "match_result", 90)

        from app.enrichment.confidence_calibrator import calibrate_confidence
        result = calibrate_confidence("match_result", 95)

        assert result.get("band") == "90-99%", (
            f"Expected band='90-99%', got '{result.get('band')}'"
        )
