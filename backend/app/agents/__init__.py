"""Agentic layer for the Fibre Forecast System.

A minimal, dependency-free agent runtime built on the existing OllamaClient
tool-calling method (``ollama_client.chat``). Phase 1 ships a single Stock
specialist agent; later phases add the other specialists and a supervisor that
delegates to them (see the design proposal).
"""
from app.agents.base import Tool, AgentResult, run_agent

__all__ = ["Tool", "AgentResult", "run_agent"]
