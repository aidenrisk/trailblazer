"""Automated carrier login, one class per carrier.

`resolve_login(carrier_id)` returns the object that signs into that portal. Each
carrier module is imported here so registering is a side effect of importing the
package: a class in a module nobody imported would not be in the registry, and
the failure would be a carrier silently having no login.
"""

from trailblazer.agents.login.base import CarrierLogin, LoginError, LoginResult
from trailblazer.agents.login.registry import register, registered, resolve_login

from trailblazer.agents.login import pie as _pie  # noqa: F401  (registers PieLogin)

__all__ = [
    "CarrierLogin",
    "LoginError",
    "LoginResult",
    "register",
    "registered",
    "resolve_login",
]
