"""Provider-specific news ingestion adapters."""


class ProviderUnavailableError(RuntimeError):
    """A provider could not supply valid input during one refresh."""

