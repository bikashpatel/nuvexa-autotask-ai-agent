"""Select the analysis agent implementation from process configuration."""

from __future__ import annotations

from app.agents.base import EmailAnalysisAgent
from app.agents.heuristic import HeuristicAnalysisAgent
from app.core.config import LLMProvider, Settings


def build_analysis_agent(settings: Settings) -> EmailAnalysisAgent:
    """Heuristic agent for `LLM_PROVIDER=heuristic`, otherwise the OpenAI / Azure OpenAI agent."""
    if settings.llm_provider is LLMProvider.HEURISTIC:
        return HeuristicAnalysisAgent()
    from app.agents.openai_agent import OpenAIAnalysisAgent

    return OpenAIAnalysisAgent(settings)
