"""Competition Special Analysis — matchday completion detector, context assembler,
Ollama prompt builder, and persistence layer.

This module never modifies any existing module except via the scheduler job
registration in scheduler.py and the context injection in ai_prediction_pipeline.py.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.storage.db import db_conn
from app.storage.db import DB_PATH
from app.storage.league_memory import _init_db

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"finished", "cancelled", "postponed"}


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class CompletedRound:
    competition_key: str
    round_name: str
    match_count: int


@dataclass
class StandingRow:
    position: int
    team: str
    played: int
    points: int
    goal_difference: int


@dataclass
class RoundResult:
    match_name: str
    score_home: str
    score_away: str
    was_upset: bool


@dataclass
class TeamOddsMovement:
    team: str
    opening: float
    current: float
    direction: str  # "shortened" | "drifted" | "stable"


@dataclass
class AnalysisContext:
    competition_key: str
    round_name: str
    match_count: int
    standings: list[StandingRow] = field(default_factory=list)
    form: dict[str, str] = field(default_factory=dict)
    round_results: list[RoundResult] = field(default_factory=list)
    odds_movements: list[TeamOddsMovement] = field(default_factory=list)
    matchday_date: str = ""


# ── Table DDL ─────────────────────────────────────────────────────────────────

def init_competition_analysis_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists competition_analysis (
            id integer primary key autoincrement,
            competition_key text not null,
            round_name text not null,
            analysis_text text not null,
            model_used text not null,
            match_count integer not null default 0,
            matchday_date text not null default '',
            generated_at text not null
        )
        """
    )
    conn.execute(
        "create index if not exists idx_comp_analysis_key_date "
        "on competition_analysis(competition_key, generated_at desc)"
    )


# ── Persistence ───────────────────────────────────────────────────────────────

def persist_competition_analysis(
    conn: sqlite3.Connection,
    competition_key: str,
    round_name: str,
    analysis_text: str,
    model_used: str,
    match_count: int,
    matchday_date: str,
    generated_at: str,
) -> None:
    """Always INSERT a new row — never updates an existing one."""
    conn.execute(
        """
        insert into competition_analysis
            (competition_key, round_name, analysis_text, model_used,
             match_count, matchday_date, generated_at)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (competition_key, round_name, analysis_text, model_used,
         match_count, matchday_date, generated_at),
    )


def get_latest_analysis(competition_key: str, conn: sqlite3.Connection) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        select * from competition_analysis
        where competition_key = ?
        order by generated_at desc
        limit 1
        """,
        (competition_key,),
    ).fetchone()
    return dict(row) if row else None


def get_analysis_history(
    competition_key: str, limit: int, conn: sqlite3.Connection
) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select * from competition_analysis
        where competition_key = ?
        order by generated_at desc
        limit ?
        """,
        (competition_key, limit),
    ).fetchall()
    return [dict(row) for row in rows]


# ── Matchday Completion Detector ──────────────────────────────────────────────

def is_round_complete(statuses: list[str]) -> bool:
    return bool(statuses) and all(s in _TERMINAL_STATUSES for s in statuses)


def should_skip_small_round(match_count: int) -> bool:
    return match_count < 2


def should_generate_analysis(round_complete: bool, prior_analysis_exists: bool) -> bool:
    return round_complete and not prior_analysis_exists


def detect_newly_completed_rounds(conn: sqlite3.Connection) -> list[CompletedRound]:
    conn.row_factory = sqlite3.Row
    # Fetch all enabled competitions
    enabled = conn.execute(
        "select key from competition_special_settings where enabled = 1"
    ).fetchall()

    results: list[CompletedRound] = []
    for setting in enabled:
        key = setting["key"]
        # Group buffer rows by round_name
        rounds = conn.execute(
            """
            select round_name, count(*) as cnt,
                   group_concat(status, '|') as statuses
            from competition_special_buffer
            where competition_key = ?
              and round_name is not null and round_name != ''
            group by round_name
            order by round_name asc
            """,
            (key,),
        ).fetchall()

        for row in rounds:
            round_name = row["round_name"]
            cnt = int(row["cnt"])
            statuses = (row["statuses"] or "").split("|")

            if should_skip_small_round(cnt):
                continue
            if not is_round_complete(statuses):
                continue

            # Check if analysis already exists
            existing = conn.execute(
                """
                select 1 from competition_analysis
                where competition_key = ? and round_name = ?
                limit 1
                """,
                (key, round_name),
            ).fetchone()
            if existing:
                continue

            results.append(CompletedRound(competition_key=key, round_name=round_name, match_count=cnt))

    return results


# ── Odds Movement ─────────────────────────────────────────────────────────────

def classify_odds_movement_direction(opening: float, current: float) -> str:
    diff = current - opening
    if diff <= -0.20:
        return "shortened"
    if diff >= 0.20:
        return "drifted"
    return "stable"


def _get_odds_for_match(sportybet_id: str, raw_event: dict[str, Any], conn: sqlite3.Connection) -> tuple[float | None, float | None]:
    """Return (opening, current) odds from snapshots or raw_event fallback."""
    try:
        rows = conn.execute(
            """
            select odds_json from odds_snapshots
            where sportybet_id = ?
            order by created_at asc
            """,
            (sportybet_id,),
        ).fetchall()
        if rows:
            def _extract_win_odds(odds_json: str) -> float | None:
                try:
                    data = json.loads(odds_json)
                    markets = data.get("markets") or []
                    for market in markets:
                        for sel in (market.get("selections") or []):
                            name = str(sel.get("name") or "").lower()
                            if name in ("home", "1", "win"):
                                odds = sel.get("odds")
                                if odds:
                                    return float(odds)
                except Exception:
                    pass
                return None

            opening = _extract_win_odds(rows[0][0])
            current = _extract_win_odds(rows[-1][0])
            if opening and current:
                return opening, current
    except Exception:
        pass

    # Fallback: raw_event featured odds
    try:
        featured = raw_event.get("odds_featured") or {}
        ft = featured.get("full_time") or featured.get("default") or {}
        choices = ft.get("choices") or []
        for choice in choices:
            name = str(choice.get("name") or "").lower()
            if name in ("home", "1", "win"):
                from app.competition.competition_special import _decimal_odds
                val = _decimal_odds(choice.get("fractional_value"))
                if val:
                    return val, val
    except Exception:
        pass
    return None, None


# ── Analysis Context Assembler ────────────────────────────────────────────────

def assemble_analysis_context(
    conn: sqlite3.Connection, competition_key: str, round_name: str
) -> AnalysisContext | None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        select match_id, name, score_home, score_away, status,
               raw_event, raw_detail, prediction_json, match_date
        from competition_special_buffer
        where competition_key = ? and round_name = ?
        """,
        (competition_key, round_name),
    ).fetchall()

    finished = [r for r in rows if r["status"] in _TERMINAL_STATUSES and r["status"] != "cancelled"]
    if len(finished) < 2:
        logger.warning(
            "assemble_analysis_context: fewer than 2 finished matches for %s/%s",
            competition_key, round_name,
        )
        return None

    # Build round results
    round_results: list[RoundResult] = []
    for row in finished[:6]:
        prediction = None
        try:
            prediction = json.loads(row["prediction_json"] or "null")
        except Exception:
            pass
        confidence = 0
        if isinstance(prediction, dict):
            confidence = int(prediction.get("confidence") or 0)
        was_upset = confidence >= 60  # high-confidence prediction that may have been wrong
        round_results.append(RoundResult(
            match_name=row["name"] or "",
            score_home=row["score_home"] or "",
            score_away=row["score_away"] or "",
            was_upset=was_upset,
        ))

    # Build standings from raw_detail of first enriched match
    standings: list[StandingRow] = []
    form: dict[str, str] = {}
    for row in rows:
        if not row["raw_detail"]:
            continue
        try:
            detail = json.loads(row["raw_detail"])
            raw_standings = detail.get("standings") or []
            if raw_standings and not standings:
                for i, s in enumerate(raw_standings[:8]):
                    team_obj = s.get("team") or {}
                    team_name = team_obj.get("name") or str(team_obj) if isinstance(team_obj, dict) else str(team_obj)
                    standings.append(StandingRow(
                        position=int(s.get("position") or i + 1),
                        team=team_name,
                        played=int(s.get("played") or 0),
                        points=int(s.get("points") or 0),
                        goal_difference=int(str(s.get("goal_diff") or "0").replace("+", "") or 0),
                    ))
            # Form strings from last_matches
            for side in ("home_last_matches", "away_last_matches"):
                matches = detail.get(side) or []
                if not matches:
                    continue
                event = json.loads(row["raw_event"] or "{}")
                team_key = "home_team" if side == "home_last_matches" else "away_team"
                team_obj = event.get(team_key) or {}
                team_name = team_obj.get("name") if isinstance(team_obj, dict) else str(team_obj)
                if team_name and team_name not in form:
                    results = []
                    for m in matches[:5]:
                        score = m.get("score") or {}
                        hg = score.get("home")
                        ag = score.get("away")
                        if hg is None or ag is None:
                            continue
                        try:
                            results.append("W" if int(hg) > int(ag) else "D" if int(hg) == int(ag) else "L")
                        except Exception:
                            pass
                    if results:
                        form[team_name] = "".join(results)
        except Exception:
            continue

    # Odds movement
    odds_movements: list[TeamOddsMovement] = []
    for row in finished:
        try:
            event = json.loads(row["raw_event"] or "{}")
            sportybet_id = f"competition:{competition_key}:{row['match_id']}"
            opening, current = _get_odds_for_match(sportybet_id, event, conn)
            if opening and current:
                direction = classify_odds_movement_direction(opening, current)
                if direction != "stable":
                    home_team = event.get("home_team") or {}
                    team_name = home_team.get("name") if isinstance(home_team, dict) else str(home_team)
                    odds_movements.append(TeamOddsMovement(
                        team=team_name or row["name"] or "",
                        opening=opening,
                        current=current,
                        direction=direction,
                    ))
        except Exception:
            continue

    matchday_date = ""
    for row in finished:
        if row["match_date"]:
            matchday_date = row["match_date"]
            break

    return AnalysisContext(
        competition_key=competition_key,
        round_name=round_name,
        match_count=len(finished),
        standings=standings,
        form=form,
        round_results=round_results,
        odds_movements=odds_movements,
        matchday_date=matchday_date,
    )


# ── Prompt Builder ────────────────────────────────────────────────────────────

def _standings_compact(standings: list[StandingRow], max_teams: int = 8) -> str:
    parts = [f"{s.position}.{s.team[:12]}({s.points}pts,GD{s.goal_difference:+d})" for s in standings[:max_teams]]
    return "|".join(parts)[:160]


def _form_compact(form: dict[str, str]) -> str:
    parts = [f"{team[:10]}:{results}" for team, results in list(form.items())[:6] if results]
    return "|".join(parts)[:90]


def _results_compact(results: list[RoundResult]) -> str:
    parts = []
    for r in results[:6]:
        upset = "!" if r.was_upset else ""
        parts.append(f"{r.match_name[:16]}:{r.score_home}-{r.score_away}{upset}")
    return "|".join(parts)[:120]


def _odds_compact(movements: list[TeamOddsMovement]) -> str:
    parts = [f"{m.team[:10]}:{m.direction[0].upper()}({m.opening:.2f}→{m.current:.2f})" for m in movements]
    return "|".join(parts)[:80]


def build_analysis_prompt(ctx: AnalysisContext) -> str:
    standings_str = _standings_compact(ctx.standings, max_teams=8)
    form_str = _form_compact(ctx.form)
    results_str = _results_compact(ctx.round_results)
    odds_str = _odds_compact(ctx.odds_movements)

    prompt = (
        f"Competition:{ctx.competition_key} Round:{ctx.round_name} Matches:{ctx.match_count}\n"
        f"Standings:{standings_str}\n"
        f"Form:{form_str}\n"
        f"Results:{results_str}\n"
        f"OddsMovement:{odds_str}\n"
        "Provide a structured JSON analysis of this matchday with:\n"
        "- \"analysis\": 2-3 sentence summary of key results and standings impact\n"
        "- \"top_table\": array of top 3-5 teams with position and points (e.g. [{\"pos\":1,\"team\":\"Name\",\"pts\":12}]) \n"
        "- \"disappointments\": array of teams or results that disappointed this week (e.g. [\"Team X dropped to 5th\", \"Y lost at home\"]) \n"
        "- \"standings_impact\": brief note on the biggest standings changes\n"
        "JSON: {\"analysis\":\"...\", \"top_table\":[...], \"disappointments\":[...], \"standings_impact\":\"...\"}"
    )

    # Token budget check (len // 4 as proxy)
    if len(prompt) // 4 >= 250:
        standings_str = _standings_compact(ctx.standings, max_teams=6)
        prompt = (
            f"Competition:{ctx.competition_key} Round:{ctx.round_name} Matches:{ctx.match_count}\n"
            f"Standings:{standings_str}\n"
            f"Form:{form_str}\n"
            f"Results:{results_str}\n"
            f"OddsMovement:{odds_str}\n"
            "Provide a structured JSON analysis of this matchday with:\n"
            "- \"analysis\": 2-3 sentence summary\n"
            "- \"top_table\": array of top 3 teams with position and points\n"
            "- \"disappointments\": array of teams/results that disappointed\n"
            "- \"standings_impact\": brief note on standings changes\n"
            "JSON: {\"analysis\":\"...\", \"top_table\":[...], \"disappointments\":[...], \"standings_impact\":\"...\"}"
        )

    return prompt


# ── LLM Integration ─────────────────────────────────────────────────────────

def run_competition_analysis(
    competition_key: str, round_name: str | None = None
) -> dict[str, Any]:
    from app.ai.llm_agent import is_llm_available
    from app.ai.llm_agent import _call_llm

    # Determine round_name if not provided
    _init_db()
    with db_conn(timeout=30) as conn:
        init_competition_analysis_table(conn)
        if round_name is None:
            rounds = detect_newly_completed_rounds(conn)
            target = next((r for r in rounds if r.competition_key == competition_key), None)
            if not target:
                return {"status": "no_completed_rounds", "competition_key": competition_key}
            round_name = target.round_name

        ctx = assemble_analysis_context(conn, competition_key, round_name)

    if ctx is None:
        logger.warning("run_competition_analysis: no context for %s/%s", competition_key, round_name)
        return {"status": "insufficient_data", "competition_key": competition_key, "round_name": round_name}

    logger.info(
        "run_competition_analysis: key=%s round=%s matches=%d",
        competition_key, round_name, ctx.match_count,
    )

    # Model priority
    model = next(
        (name for name in ("openrouter",) if is_llm_available(name)),
        None,
    )
    if not model:
        logger.warning(
            "run_competition_analysis: LLM unavailable for %s/%s", competition_key, round_name
        )
        return {"status": "llm_unavailable", "competition_key": competition_key, "round_name": round_name}

    prompt = build_analysis_prompt(ctx)
    logger.debug(
        "run_competition_analysis: model=%s estimated_tokens=%d start=%s",
        model, len(prompt) // 4, datetime.now(timezone.utc).isoformat(),
    )

    try:
        started = time.monotonic()
        raw = _call_llm(model, prompt, timeout=45)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "run_competition_analysis: elapsed_ms=%d preview=%s",
            elapsed_ms, raw[:80],
        )
    except Exception as exc:
        logger.warning(
            "run_competition_analysis: LLM call failed for %s/%s: %s",
            competition_key, round_name, exc,
        )
        return {"status": "llm_error", "competition_key": competition_key, "round_name": round_name, "error": str(exc)}

    # Extract structured analysis from JSON response
    analysis_text = raw.strip()
    analysis_data: dict[str, Any] = {}
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end])
            analysis_data = parsed if isinstance(parsed, dict) else {}
            analysis_text = analysis_data.get("analysis") or analysis_text
    except Exception:
        pass

    # Store the full structured JSON so the frontend can render top_table and disappointments
    if analysis_data:
        analysis_text = json.dumps(analysis_data)

    generated_at = datetime.now(timezone.utc).isoformat()
    _init_db()
    with db_conn(timeout=30) as conn:
        init_competition_analysis_table(conn)
        persist_competition_analysis(
            conn,
            competition_key=competition_key,
            round_name=round_name,
            analysis_text=analysis_text,
            model_used=model,
            match_count=ctx.match_count,
            matchday_date=ctx.matchday_date,
            generated_at=generated_at,
        )
        conn.commit()

    return {
        "status": "ok",
        "competition_key": competition_key,
        "round_name": round_name,
        "analysis_text": analysis_text,
        "analysis_data": analysis_data,
        "generated_at": generated_at,
        "match_count": ctx.match_count,
        "model_used": model,
    }


# ── Catch-up: refresh stale/in-progress match scores ─────────────────────────

def catchup_competition_scores(competition_key: str) -> dict[str, Any]:
    """Re-fetch SofaScore status for rows that are still in-progress or have no
    terminal status — handles the case where the engine was offline during a match.
    Returns counts of rows refreshed and newly finished.
    """
    from app.data_clients.sofascore_client import fetch_event

    _init_db()
    refreshed = newly_finished = 0
    with db_conn(timeout=30) as conn:
        init_competition_analysis_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select match_id, raw_event, status from competition_special_buffer
            where competition_key = ?
              and status not in ('finished', 'cancelled', 'postponed')
            """,
            (competition_key,),
        ).fetchall()

        for row in rows:
            try:
                fresh = fetch_event(row["match_id"])
                if not fresh:
                    continue
                status_obj = fresh.get("status") or {}
                new_status = str(status_obj.get("type") or status_obj.get("description") or "").lower()
                score = fresh.get("score") or {}
                conn.execute(
                    """
                    update competition_special_buffer
                    set status = ?, score_home = ?, score_away = ?, raw_event = ?
                    where competition_key = ? and match_id = ?
                    """,
                    (
                        new_status,
                        str(score.get("home") or ""),
                        str(score.get("away") or ""),
                        json.dumps(fresh),
                        competition_key,
                        row["match_id"],
                    ),
                )
                refreshed += 1
                if new_status in _TERMINAL_STATUSES:
                    newly_finished += 1
            except Exception as exc:
                logger.debug("catchup_competition_scores: %s/%s failed: %s", competition_key, row["match_id"], exc)
        conn.commit()

    return {"refreshed": refreshed, "newly_finished": newly_finished}


def _has_new_finished_since_last_analysis(conn: sqlite3.Connection, competition_key: str) -> bool:
    """Return True if any match finished after the most recent analysis was generated."""
    row = conn.execute(
        """
        select max(generated_at) from competition_analysis
        where competition_key = ?
        """,
        (competition_key,),
    ).fetchone()
    last_generated_at = row[0] if row else None
    if not last_generated_at:
        # No analysis yet — check if there are any finished matches at all
        count = conn.execute(
            """
            select count(*) from competition_special_buffer
            where competition_key = ? and status in ('finished', 'cancelled', 'postponed')
            """,
            (competition_key,),
        ).fetchone()[0]
        return int(count or 0) > 0

    # Check if any match was updated (enriched_at) after the last analysis
    count = conn.execute(
        """
        select count(*) from competition_special_buffer
        where competition_key = ?
          and status in ('finished', 'cancelled', 'postponed')
          and enriched_at > ?
        """,
        (competition_key, last_generated_at),
    ).fetchone()[0]
    return int(count or 0) > 0


# ── Scheduler Job ─────────────────────────────────────────────────────────────

def job_competition_analysis() -> dict[str, Any]:
    from app.scheduling.pipeline_registry import is_pipeline_enabled
    from app.utils.activity_log import record_activity

    if not is_pipeline_enabled("competition_analysis"):
        return {"status": "skipped", "reason": "pipeline_disabled"}

    record_activity("Competition analysis job started", job="competition_analysis", status="running")

    rounds_analysed = 0
    rounds_skipped = 0
    rounds_deferred = 0
    catchup_total = 0
    errors: list[str] = []
    competitions_checked = 0

    try:
        _init_db()

        # Step 1: catch up any stale/in-progress rows for all enabled competitions
        with db_conn(timeout=30) as conn:
            init_competition_analysis_table(conn)
            enabled_keys = [
                row[0] for row in conn.execute(
                    "select key from competition_special_settings where enabled = 1"
                ).fetchall()
            ]

        for key in enabled_keys:
            try:
                catchup = catchup_competition_scores(key)
                catchup_total += catchup.get("newly_finished", 0)
            except Exception as exc:
                logger.debug("job_competition_analysis: catchup failed for %s: %s", key, exc)

        # Step 2: detect completed rounds and generate analysis
        with db_conn(timeout=30) as conn:
            init_competition_analysis_table(conn)
            completed_rounds = detect_newly_completed_rounds(conn)

            # Defer competitions where nothing new has finished since last analysis
            actionable: list[CompletedRound] = []
            for completed in completed_rounds:
                if _has_new_finished_since_last_analysis(conn, completed.competition_key):
                    actionable.append(completed)
                else:
                    rounds_deferred += 1
                    logger.info(
                        "job_competition_analysis: deferring %s/%s — no new finished matches",
                        completed.competition_key, completed.round_name,
                    )

        competitions_checked = len({r.competition_key for r in actionable})
        logger.info(
            "job_competition_analysis: competitions_checked=%d actionable=%d deferred=%d catchup_new=%d",
            competitions_checked, len(actionable), rounds_deferred, catchup_total,
        )

        for completed in actionable:
            try:
                result = run_competition_analysis(
                    completed.competition_key, completed.round_name
                )
                if result.get("status") == "ok":
                    rounds_analysed += 1
                else:
                    rounds_skipped += 1
                    logger.warning(
                        "job_competition_analysis: skipped %s/%s reason=%s",
                        completed.competition_key, completed.round_name, result.get("status"),
                    )
            except Exception as exc:
                errors.append(f"{completed.competition_key}/{completed.round_name}: {exc}")
                logger.exception("job_competition_analysis: error for %s/%s", completed.competition_key, completed.round_name)

    except Exception as exc:
        errors.append(str(exc))
        record_activity(
            f"Competition analysis job failed: {exc}",
            job="competition_analysis",
            status="error",
        )
        return {
            "status": "error",
            "job": "competition_analysis",
            "competitions_checked": competitions_checked,
            "rounds_analysed": rounds_analysed,
            "rounds_skipped": rounds_skipped,
            "rounds_deferred": rounds_deferred,
            "catchup_newly_finished": catchup_total,
            "errors": errors,
        }

    final_status = "ok" if not errors else "degraded"
    record_activity(
        f"Competition analysis job done: {rounds_analysed} analysed, {rounds_skipped} skipped, {rounds_deferred} deferred",
        job="competition_analysis",
        status=final_status,
        details={
            "rounds_analysed": rounds_analysed,
            "rounds_skipped": rounds_skipped,
            "rounds_deferred": rounds_deferred,
            "catchup_newly_finished": catchup_total,
            "errors": errors,
        },
    )

    # Rebuild competition stat profiles after every analysis run so the
    # competition_intelligence signal always has fresh baseline rates.
    stat_profile_result: dict[str, Any] = {}
    try:
        stat_profile_result = rebuild_competition_stat_profiles()
    except Exception as exc:
        logger.warning("job_competition_analysis: stat profile rebuild failed: %s", exc)

    return {
        "status": final_status,
        "job": "competition_analysis",
        "competitions_checked": competitions_checked,
        "rounds_analysed": rounds_analysed,
        "rounds_skipped": rounds_skipped,
        "rounds_deferred": rounds_deferred,
        "catchup_newly_finished": catchup_total,
        "stat_profiles": stat_profile_result,
        "errors": errors,
    }


# ── Competition Stat Profile Builder ─────────────────────────────────────────
#
# Computes per-competition structural statistics from the ``matches`` table
# (all finished matches observed by the league-memory recorder) and writes
# them into ``competition_stat_profiles``.
#
# Call this:
#   • at the end of ``job_competition_analysis()`` (automatic, wired below)
#   • manually: ``from app.competition.competition_analyser import
#                   rebuild_competition_stat_profiles``
#
# The profile is then consumed by the ``competition_intelligence`` signal in
# ``enriched_prediction.py`` to give each league its own baseline rates
# instead of global defaults.

def rebuild_competition_stat_profiles(
    min_matches: int = 10,
    competition_key: str | None = None,
) -> dict[str, Any]:
    """Recompute stat profiles for all (or one) competition from finished matches.

    Parameters
    ----------
    min_matches:
        Minimum finished matches required to write a profile row.
        Competitions with fewer samples get skipped to avoid noisy rates.
    competition_key:
        When given, rebuild only that competition. Otherwise rebuild all.

    Returns
    -------
    dict with ``profiles_written``, ``profiles_skipped``, ``competition_keys``.
    """
    _init_db()
    profiles_written = 0
    profiles_skipped = 0
    competition_keys_done: list[str] = []

    with db_conn(timeout=60) as conn:
        conn.row_factory = sqlite3.Row

        # ── 1. Discover which competitions to process ─────────────────────
        if competition_key:
            league_rows = conn.execute(
                """
                select distinct league_key, league_name
                from matches
                where is_finished = 1 and league_key = ?
                """,
                (competition_key,),
            ).fetchall()
        else:
            league_rows = conn.execute(
                """
                select distinct league_key, league_name
                from matches
                where is_finished = 1
                """
            ).fetchall()

        for league_row in league_rows:
            lkey  = league_row["league_key"]
            lname = league_row["league_name"] or lkey

            try:
                written = _build_one_profile(conn, lkey, lname, min_matches)
                if written:
                    profiles_written += 1
                    competition_keys_done.append(lkey)
                else:
                    profiles_skipped += 1
            except Exception as exc:
                logger.warning("rebuild_competition_stat_profiles: failed for %s: %s", lkey, exc)
                profiles_skipped += 1

        conn.commit()

    logger.info(
        "rebuild_competition_stat_profiles: written=%d skipped=%d",
        profiles_written, profiles_skipped,
    )
    return {
        "status": "ok",
        "profiles_written": profiles_written,
        "profiles_skipped": profiles_skipped,
        "competition_keys": competition_keys_done,
    }


def _build_one_profile(
    conn: sqlite3.Connection,
    league_key: str,
    league_name: str,
    min_matches: int,
) -> bool:
    """Compute and upsert a single competition's stat profile. Returns True if written."""
    from app.utils.primitives import _loads, _to_int

    # ── Fetch all finished matches for this competition ───────────────────
    rows = conn.execute(
        """
        select final_home_goals, final_away_goals,
               half_time_home_goals, half_time_away_goals,
               goal_times_json
        from matches
        where league_key = ? and is_finished = 1
          and final_home_goals is not null
          and final_away_goals is not null
        """,
        (league_key,),
    ).fetchall()

    n = len(rows)
    if n < min_matches:
        return False

    # ── Accumulators ──────────────────────────────────────────────────────
    home_wins = draws = away_wins = 0
    btts = over_15 = over_25 = over_35 = 0
    total_goals = home_goals_sum = away_goals_sum = 0

    # HT → FT transition buckets
    # key: (ht_state, ft_state) where state is 'H', 'D', 'A'
    ht_ft: dict[tuple[str, str], int] = {}

    # Comeback counts
    home_comebacks = away_comebacks = 0  # lost at HT, won FT
    matches_with_ht = 0

    # Late goal: goal in minute >= 80
    late_goal_matches = 0
    matches_with_goal_times = 0

    for row in rows:
        fh = _to_int(row["final_home_goals"])
        fa = _to_int(row["final_away_goals"])
        ht_h = row["half_time_home_goals"]
        ht_a = row["half_time_away_goals"]
        goal_times = _loads(row["goal_times_json"] or "[]", [])

        # FT outcome
        if fh > fa:
            home_wins += 1
        elif fa > fh:
            away_wins += 1
        else:
            draws += 1

        total = fh + fa
        total_goals += total
        home_goals_sum += fh
        away_goals_sum += fa

        if fh > 0 and fa > 0:
            btts += 1
        if total >= 2:
            over_15 += 1
        if total >= 3:
            over_25 += 1
        if total >= 4:
            over_35 += 1

        # HT → FT transitions (only when HT score is available)
        if ht_h is not None and ht_a is not None:
            matches_with_ht += 1
            ht_h = _to_int(ht_h)
            ht_a = _to_int(ht_a)
            ht_state = "H" if ht_h > ht_a else "A" if ht_a > ht_h else "D"
            ft_state = "H" if fh > fa else "A" if fa > fh else "D"
            key = (ht_state, ft_state)
            ht_ft[key] = ht_ft.get(key, 0) + 1

            # Comebacks
            if ht_state == "A" and ft_state == "H":
                home_comebacks += 1
            if ht_state == "H" and ft_state == "A":
                away_comebacks += 1

        # Late goals — goal_times_json is a list of minute values or dicts
        if goal_times:
            matches_with_goal_times += 1
            had_late = False
            for gt in goal_times:
                minute = (
                    _to_int(gt.get("minute") if isinstance(gt, dict) else gt)
                )
                if minute >= 80:
                    had_late = True
                    break
            if had_late:
                late_goal_matches += 1

    def _rate(num: int, den: int) -> float | None:
        return round(num / den, 4) if den > 0 else None

    def _ht_rate(ht: str, ft: str, base: str) -> float | None:
        """P(ft_state | ht_state): conditioned on ht_state = base."""
        ht_base_total = sum(v for (h, _), v in ht_ft.items() if h == base)
        return _rate(ht_ft.get((ht, ft), 0), ht_base_total)

    # ── Prediction win rate for this competition ──────────────────────────
    pred_row = conn.execute(
        """
        select count(*) as total,
               sum(case when result = 'win' then 1 else 0 end) as wins
        from prediction_history
        where (league_name = ? or league_name like ?)
          and graded_at is not null
          and result in ('win', 'loss')
          and pick_type != 'no_bet'
        """,
        (league_name, f"%{league_key}%"),
    ).fetchone()
    pred_total = _to_int(pred_row["total"] if pred_row else 0)
    pred_wins  = _to_int(pred_row["wins"]  if pred_row else 0)

    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        insert into competition_stat_profiles (
            competition_key, competition_name,
            home_win_rate, draw_rate, away_win_rate,
            btts_rate, over_1_5_rate, over_2_5_rate, over_3_5_rate,
            avg_goals_per_match, avg_home_goals, avg_away_goals,
            ht_home_win_to_ft_win, ht_draw_to_ft_home_win, ht_draw_to_ft_draw,
            ht_draw_to_ft_away_win, ht_away_win_to_ft_win,
            home_comeback_rate, away_comeback_rate, late_goal_rate,
            prediction_win_rate, prediction_samples,
            sample_size, last_computed
        ) values (
            :key, :name,
            :home_win_rate, :draw_rate, :away_win_rate,
            :btts_rate, :over_1_5_rate, :over_2_5_rate, :over_3_5_rate,
            :avg_goals_per_match, :avg_home_goals, :avg_away_goals,
            :ht_home_win_to_ft_win, :ht_draw_to_ft_home_win, :ht_draw_to_ft_draw,
            :ht_draw_to_ft_away_win, :ht_away_win_to_ft_win,
            :home_comeback_rate, :away_comeback_rate, :late_goal_rate,
            :prediction_win_rate, :prediction_samples,
            :sample_size, :last_computed
        )
        on conflict(competition_key) do update set
            competition_name        = excluded.competition_name,
            home_win_rate           = excluded.home_win_rate,
            draw_rate               = excluded.draw_rate,
            away_win_rate           = excluded.away_win_rate,
            btts_rate               = excluded.btts_rate,
            over_1_5_rate           = excluded.over_1_5_rate,
            over_2_5_rate           = excluded.over_2_5_rate,
            over_3_5_rate           = excluded.over_3_5_rate,
            avg_goals_per_match     = excluded.avg_goals_per_match,
            avg_home_goals          = excluded.avg_home_goals,
            avg_away_goals          = excluded.avg_away_goals,
            ht_home_win_to_ft_win   = excluded.ht_home_win_to_ft_win,
            ht_draw_to_ft_home_win  = excluded.ht_draw_to_ft_home_win,
            ht_draw_to_ft_draw      = excluded.ht_draw_to_ft_draw,
            ht_draw_to_ft_away_win  = excluded.ht_draw_to_ft_away_win,
            ht_away_win_to_ft_win   = excluded.ht_away_win_to_ft_win,
            home_comeback_rate      = excluded.home_comeback_rate,
            away_comeback_rate      = excluded.away_comeback_rate,
            late_goal_rate          = excluded.late_goal_rate,
            prediction_win_rate     = excluded.prediction_win_rate,
            prediction_samples      = excluded.prediction_samples,
            sample_size             = excluded.sample_size,
            last_computed           = excluded.last_computed
        """,
        {
            "key":   league_key,
            "name":  league_name,
            "home_win_rate":           _rate(home_wins, n),
            "draw_rate":               _rate(draws, n),
            "away_win_rate":           _rate(away_wins, n),
            "btts_rate":               _rate(btts, n),
            "over_1_5_rate":           _rate(over_15, n),
            "over_2_5_rate":           _rate(over_25, n),
            "over_3_5_rate":           _rate(over_35, n),
            "avg_goals_per_match":     round(total_goals / n, 4) if n else None,
            "avg_home_goals":          round(home_goals_sum / n, 4) if n else None,
            "avg_away_goals":          round(away_goals_sum / n, 4) if n else None,
            # HT → FT hold / swing rates
            "ht_home_win_to_ft_win":   _ht_rate("H", "H", "H"),
            "ht_draw_to_ft_home_win":  _ht_rate("D", "H", "D"),
            "ht_draw_to_ft_draw":      _ht_rate("D", "D", "D"),
            "ht_draw_to_ft_away_win":  _ht_rate("D", "A", "D"),
            "ht_away_win_to_ft_win":   _ht_rate("A", "A", "A"),
            # Comebacks (conditioned on matches that had a HT score)
            "home_comeback_rate":      _rate(home_comebacks, matches_with_ht),
            "away_comeback_rate":      _rate(away_comebacks, matches_with_ht),
            # Late goals (conditioned on matches that had goal-time data)
            "late_goal_rate":          _rate(late_goal_matches, matches_with_goal_times),
            # Prediction quality
            "prediction_win_rate":     _rate(pred_wins, pred_total),
            "prediction_samples":      pred_total,
            # Coverage
            "sample_size":             n,
            "last_computed":           now,
        },
    )
    return True


def get_competition_stat_profile(competition_key: str) -> dict[str, Any] | None:
    """Return the latest stat profile for a competition, or None if not yet built."""
    _init_db()
    with db_conn(timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select * from competition_stat_profiles where competition_key = ?",
            (competition_key,),
        ).fetchone()
    return dict(row) if row else None
