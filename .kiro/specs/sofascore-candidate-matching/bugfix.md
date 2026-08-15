# Bugfix Requirements Document

## Introduction

After a refactor that extracted helper functions into shared modules (`app/utils/doc_helpers.py`, `app/utils/web_helpers.py`, `app/utils/match_helpers.py`), the SofaScore candidate matching pipeline produces zero successful matches. Every scheduled SofaScore event is silently filtered out before it reaches the fuzzy-matching step, so all SportyBet matches end up with `sofascore_match_status = "no_match"` and enrichment quality degrades to "sporty_only".

Two interacting defects cause this outcome:

1. **Status field type mismatch** — `is_usable_event_for_mode` delegates to `_status_text`, which expects `event["status"]` to be a dict with a `"type"` key. When events are retrieved from the DB-backed `sofa_event_list_cache` that was written before the refactor (or when events arrive via an alternate code path that skips `_parse_event`), `status` is a raw string (e.g. `"notstarted"`). `_status_text` calls `.get("type")` on a string, which returns `None`, so `status_type` is always `""`. Because `""` is not in `LIVE_STATUS_TYPES`, every prematch event returns `False` from `is_usable_event_for_mode`, emptying the candidate list.

2. **Circular import causing silent import failure** — `enrichment.py` imports `_data_sources` from `app.storage.buffer`, and `buffer.py` calls `_event_score_for_fallback` which does a deferred import of `_event_score` from `app.enrichment.enrichment`. This circular dependency can cause Python to resolve the partially-initialised module, resulting in an `ImportError` that is silently swallowed inside `_event_score_for_fallback`'s `except Exception` handler. When this happens the fallback scorer returns `0.0` for every candidate, so `_with_search_fallback_candidates` always triggers a search and the search results are never scored above the threshold either.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a SofaScore event is fetched from the DB cache (`sofa_event_list_cache`) and its `status` field is a raw string (e.g. `"notstarted"`) rather than a parsed dict, THEN the system treats `status_type` as `""` and `is_usable_event_for_mode` returns `False` for every such event.

1.2 WHEN all candidate events return `False` from `is_usable_event_for_mode`, THEN the system produces an empty candidate list and `_fuzzy_match` has nothing to score, resulting in zero successful sofascore matches.

1.3 WHEN `enrichment.py` is imported by `buffer.py` while `buffer.py` is still being initialised (circular import), THEN the system silently returns `0.0` from `_event_score_for_fallback` for every candidate because the `ImportError` is swallowed inside the `except Exception` block.

1.4 WHEN `_event_score_for_fallback` always returns `0.0`, THEN the system incorrectly concludes that no scheduled candidate scores ≥ 0.70 and falls through to the SofaScore search fallback, which also cannot score candidates due to the same import failure, leaving `best_score = 0.0` throughout the pipeline.

### Expected Behavior (Correct)

2.1 WHEN a SofaScore event's `status` field is a raw string (e.g. `"notstarted"`, `"inprogress"`), THEN the system SHALL normalise it into the `status_type` string directly so that `is_usable_event_for_mode` returns the correct boolean without requiring the field to be a dict.

2.2 WHEN a SofaScore event is a valid scheduled (non-terminal, non-live) event regardless of whether `status` is a dict or a string, THEN the system SHALL pass it through `is_usable_event_for_mode(event, live=False)` as `True` so it enters the candidate list.

2.3 WHEN `_event_score_for_fallback` is called, THEN the system SHALL successfully invoke `_event_score` without triggering a circular import failure, so candidate scores reflect genuine fuzzy-match quality.

2.4 WHEN at least one candidate scores ≥ 0.70 in `_with_search_fallback_candidates`, THEN the system SHALL return the scheduled candidate list without falling through to the search fallback path.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a SofaScore event has a terminal status (e.g. `"finished"`, `"postponed"`, `"cancelled"`) represented either as a dict or a raw string, THEN the system SHALL CONTINUE TO return `False` from `is_usable_event_for_mode` so that finished or postponed matches are never presented as candidates.

3.2 WHEN a SofaScore event has a live status (`"inprogress"`, `"live"`) and `is_usable_event_for_mode` is called with `live=True`, THEN the system SHALL CONTINUE TO return `True` so that live enrichment candidates are unaffected.

3.3 WHEN a SofaScore event has a live status and `is_usable_event_for_mode` is called with `live=False`, THEN the system SHALL CONTINUE TO return `False` so that live events are excluded from prematch candidate lists.

3.4 WHEN a SportyBet match is a junk/simulated event (name contains markers such as `" srl"`, `"esports"`, `"virtual"`), THEN the system SHALL CONTINUE TO skip SofaScore matching for that match.

3.5 WHEN a SportyBet match already has a stored sofascore ID and a valid cached detail, THEN the system SHALL CONTINUE TO reuse the cached detail and skip re-fetching from SofaScore.

3.6 WHEN `_event_score` is invoked for two valid events with matching team names, THEN the system SHALL CONTINUE TO return a score consistent with its existing scoring formula (name, team, tournament, country, time penalty weights).

---

## Bug Condition Pseudocode

**Bug Condition Function — status type mismatch:**

```pascal
FUNCTION isBugCondition_StatusMismatch(event)
  INPUT: event of type dict (SofaScore parsed event)
  OUTPUT: boolean

  status ← event.get("status")
  RETURN isinstance(status, str)   // status is a raw string, not a dict
END FUNCTION
```

**Property: Fix Checking — prematch usability with string status:**

```pascal
FOR ALL event WHERE isBugCondition_StatusMismatch(event)
  AND event["status"] NOT IN TERMINAL_STATUS_WORDS
  AND event["status"] NOT IN LIVE_STATUS_TYPES DO

  result ← is_usable_event_for_mode'(event, live=False)
  ASSERT result = True
END FOR
```

**Bug Condition Function — circular import scorer failure:**

```pascal
FUNCTION isBugCondition_CircularImport(sporty, event)
  INPUT: sporty of type dict, event of type dict
  OUTPUT: boolean

  // Returns True when _event_score_for_fallback returns 0.0 due to import error
  // even though the event is a plausible match (same teams, same tournament)
  RETURN _event_score_for_fallback(sporty, event) = 0.0
         AND _event_score(sporty, event) > 0.0     // real score should be non-zero
END FUNCTION
```

**Property: Fix Checking — fallback scorer returns real score:**

```pascal
FOR ALL (sporty, event) WHERE plausible_match(sporty, event) DO
  result ← _event_score_for_fallback'(sporty, event)
  ASSERT result > 0.0
END FOR
```

**Preservation Goal:**

```pascal
FOR ALL event WHERE NOT isBugCondition_StatusMismatch(event) DO
  ASSERT is_usable_event_for_mode(event, live) = is_usable_event_for_mode'(event, live)
END FOR
```
