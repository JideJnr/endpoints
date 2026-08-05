"""
AI domain package.

AI and LLM layer — provider routing, Groq/Ollama/HuggingFace agents,
prompt construction, response parsing, agentic prediction pipelines.

Sub-modules are not eagerly imported here to avoid circular import chains
(several AI modules depend on app.prediction_flow, app.league_memory, etc.
which in turn may import from AI modules via shims).

Import sub-modules explicitly:
    from app.ai.ai_brain import oversee_prediction
    from app.ai.ai_router import AIRouter, get_router
    from app.ai.ai_prediction_pipeline import run_ai_prediction_with_fallback
    etc.

Or import the module object itself:
    from app.ai import ai_brain
    from app.ai import ai_router
"""
# Sub-modules available in this package:
#   ai_brain, ai_router, llm, groq_agent, ollama_agent, ollama_pipeline,
#   ollama_model_manager, chat_agent, agentic_prediction, prediction_agent,
#   ai_prediction_pipeline, ai_betbuilder, agent_tools
