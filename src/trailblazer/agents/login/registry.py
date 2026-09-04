"""Which login class serves which carrier.

A dict rather than a scan of `CarrierLogin.__subclasses__()`: a subclass defined
in a module nobody imported is invisible to a scan, and the failure is a carrier
silently falling back to the generic journey rather than an import error.
"""

from trailblazer.agents.login.base import CarrierLogin
from trailblazer.observability.logging import get_logger
from trailblazer.shared.dev_carrier_creds import CarrierCreds

log = get_logger(__name__)

_REGISTRY: dict[str, type[CarrierLogin]] = {}


def register(cls: type[CarrierLogin]) -> type[CarrierLogin]:
    """Record `cls` under its `carrier_id`. Usable as a decorator."""
    if not cls.carrier_id:
        raise ValueError(f"{cls.__name__} sets no carrier_id, so it cannot be resolved")
    if cls.carrier_id in _REGISTRY and _REGISTRY[cls.carrier_id] is not cls:
        raise ValueError(
            f"carrier_id {cls.carrier_id!r} is already served by "
            f"{_REGISTRY[cls.carrier_id].__name__}"
        )
    _REGISTRY[cls.carrier_id] = cls
    return cls


def resolve_login(carrier_id: str, creds: CarrierCreds) -> CarrierLogin:
    """The login object for `carrier_id`.

    Raises for an unknown carrier rather than returning the base journey: the
    base class has no `authenticated_selector`, so it cannot tell a successful
    sign-in from a rejected one, and a crawl that proceeded on that would
    perceive the sign-in page and record it as the application form.
    """
    cls = _REGISTRY.get(carrier_id)
    if cls is None:
        raise KeyError(
            f"no login class registered for carrier_id {carrier_id!r}; "
            f"registered: {sorted(_REGISTRY) or 'none'}"
        )
    return cls(creds)


def registered() -> list[str]:
    """Every carrier_id with a login class, for logging and for the CLI."""
    return sorted(_REGISTRY)
