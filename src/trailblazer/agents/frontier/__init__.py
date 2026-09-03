"""Frontier: the board, and the decision of what to act on next. Re-exports only."""

from trailblazer.agents.frontier.board import Board, GateWalk, gate_sides
from trailblazer.agents.frontier.frontier import Frontier

__all__ = ["Board", "Frontier", "GateWalk", "gate_sides"]
