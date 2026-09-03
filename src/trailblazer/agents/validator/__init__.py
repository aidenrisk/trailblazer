"""The Validator: runs a generated replay script and reports the outcome."""

from trailblazer.agents.validator.static_checks import static_checks
from trailblazer.agents.validator.validator import normalize_outcome, read_outcome, validate

__all__ = ["normalize_outcome", "read_outcome", "static_checks", "validate"]
