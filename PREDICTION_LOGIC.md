# PredictX Prediction Logic Notes

This project should grow from a transparent rule engine into a learning model. The rules below are intentionally small and explainable so each prediction can be tracked against final results.

## 1. Market Steam Confirmation

If a team already has a form/table/history edge and its odds have shortened from the opening price, treat that as confirmation. This does not mean "low odds always win"; it means the market moved toward the same side the model already liked.

Signals used:
- Current implied probability from odds
- Opening implied probability from `initial_fractional_value`
- Direction and size of movement
- Agreement with form/table edge

Agent behavior:
- Adds `market_steam` signal when probability moves by at least 3.5 percentage points
- Pushes the predicted side only when odds movement agrees with the underlying team edge

## 2. Live Chase / Late Goal Pressure

Late goals are more likely when the match is still close and one team must chase. A strong favorite losing or drawing after 58 minutes should increase live goal pressure, especially in leagues already profiled as goal-friendly.

Signals used:
- Minute
- Current score difference
- Pregame/form strength edge
- Recent goal pressure
- League late-goal profile

Agent behavior:
- Adds `live_chase_pressure` when the game is live, after 58 minutes, and the score difference is 0 or 1
- Promotes `Next goal / Over 0.5 live` when the chasing pressure is strong
- Uses league memory to raise or lower confidence once the system has observed resolved late-goal snapshots for that league

## 2b. League Memory Table

The agent stores every live snapshot it sees, not only late-game situations. Each snapshot keeps:
- League
- Minute and minute bucket
- Score and score state
- Favorite side and favorite probability when odds are available
- Red-card state
- Teams and source

When the same match is later observed as finished, the agent resolves every snapshot into outcomes:
- Next goal happened
- Over 0.5, 1.5, 2.5, and 3.5 final goals
- Home/draw/away result
- Favorite won
- Favorite recovered from drawing or losing
- Red-card team conceded after the snapshot

The older late-goal table is still kept as a focused shortcut for:
- Minute is 70 or later
- Score difference is 0 or 1

Example learned stat:
- "LaLiga late goals after 70' with score diff <= 1: 8 hits from 13 observed snapshots = 61.5%"
- "LaLiga, 71-80, favorite drawing: next goal happened in 58% of snapshots"

Memory is stored locally in `data/predictx_memory.sqlite3`.

## 3. Red Card State

A red card should change the match state immediately. The model does not simply pick against the team with the red card; it checks whether the card hit the favorite, underdog, leading team, or chasing team.

Signals used:
- Home/away red card count
- Model edge before card
- Current score and minute

Agent behavior:
- Adds a `red_card` pick when card counts differ
- Warns when the favorite is weakened rather than blindly backing the original favorite

## Research Sources

- Opta Analyst: expected goals measure chance quality using historical shots and context: https://theanalyst.com/2023/08/what-is-expected-goals-xg
- Stochastic modelling of football matches: red cards reduce the affected team's goal intensity by more than 30%, and losing teams increase goal rate while chasing: https://arxiv.org/abs/2312.04338
- Closing line value concept and odds snapshots are commonly used for market calibration: https://www.thestatsapi.com/odds-api/clv
