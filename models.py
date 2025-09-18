"""Dataclass models for the Teradata MCP Data Quality orchestrator.

Each model captures a stable shape used across orchestration phases, reducing
implicit dict contracts and improving type clarity.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Any

@dataclass(slots=True)
class Intent:
    goal: str
    target_patterns: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)

@dataclass(slots=True)
class DiscoveryStep:
    tool: str
    why: str | None = None

@dataclass(slots=True)
class DiscoveryPlan:
    steps: List[DiscoveryStep] = field(default_factory=list)

@dataclass(slots=True)
class QualityToolSpec:
    tool: str
    reason: str | None = None

@dataclass(slots=True)
class QualityPlan:
    dq_tools: List[QualityToolSpec] = field(default_factory=list)

@dataclass(slots=True)
class DiscoveryResults:
    databases: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    ddl: Dict[str, str] = field(default_factory=dict)
    previews: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

@dataclass(slots=True)
class Summary:
    summary: str
    issues: List[Any] = field(default_factory=list)
    recommendations: List[Any] = field(default_factory=list)
