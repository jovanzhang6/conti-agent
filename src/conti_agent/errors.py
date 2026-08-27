from __future__ import annotations


class ContiAgentError(Exception):
    """Base class for expected runtime failures."""


class ConfigurationError(ContiAgentError):
    pass


class ProviderError(ContiAgentError):
    """A model-provider request failed."""

    def __init__(self, message: str, *, transient: bool = False,
                 status_code: int | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.status_code = status_code


class ToolValidationError(ContiAgentError):
    pass


class ToolExecutionError(ContiAgentError):
    pass


class PermissionDenied(ContiAgentError):
    pass


class AgentIterationLimit(ContiAgentError):
    pass
