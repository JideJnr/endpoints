# ai

## Purpose

This domain contains all AI and LLM-powered logic. It manages provider routing (OpenRouter → HuggingFace → Ollama → rules-based fallback), constructs prompts from enriched prediction context, parses and validates model responses, and exposes agentic prediction pipelines consumed by scheduling and routers.

## Member Modules

| Module | Responsibility |
|---|---|
| `ai_brain.py` | Top-level AI orchestrator — selects provider, dispatches prediction requests, aggregates responses |
| `groq_agent.py` | Groq API client and prompt execution wrapper |
| `ollama_agent.py` | Ollama local-model client and prompt execution wrapper |
| `ollama_pipeline.py` | Multi-step pipeline that chains Ollama model calls for complex predictions |
| `ollama_model_manager.py` | Manages Ollama model availability, pulls, and health checks |
| `chat_agent.py` | Conversational agent for interactive prediction queries via chat interface |
| `agentic_prediction.py` | Agentic loop that iteratively refines predictions using tool calls and self-critique |
| `prediction_agent.py` | Single-shot prediction agent that wraps enriched context into a structured LLM prompt |
| `ai_prediction_pipeline.py` | End-to-end AI prediction pipeline integrating all agent stages |
| `ai_betbuilder.py` | AI-driven bet-builder that constructs multi-leg accumulators from individual predictions |
| `ai_router.py` | Provider routing logic with fallback chain and cost/latency optimisation |
| `agent_tools.py` | Tool definitions (function-calling schemas) available to agentic loops |
| `llm.py` | Low-level LLM abstraction layer — handles token budgets, streaming, and response parsing |

## Dependency Direction

**Depends on:** `config`, `storage`, `data_clients`, `models`, `enrichment`, `risk`, `market`, `competition`, `team_watcher`

**Depended on by:** `scheduling`, `routers`

## Notes

Provider routing in `ai_router.py` should be transparent to callers — consumers invoke `ai_brain` without specifying the underlying model. API keys and endpoint URLs are read exclusively from `config`. LLM response parsing must be resilient to malformed output; never raise an unhandled exception on a bad LLM response.
