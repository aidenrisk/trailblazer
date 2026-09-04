"""Frontier: the board, and the decision of what to act on next. Re-exports only."""

from trailblazer.agents.frontier.board import Board, GateWalk, gate_sides
from trailblazer.agents.frontier.frontier import MAX_RESTARTS, Frontier

__all__ = ["MAX_RESTARTS", "Board", "Frontier", "GateWalk", "gate_sides"]
