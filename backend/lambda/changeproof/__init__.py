"""ChangeProof verification engine.

Phase 0: fully local. Nothing in this package imports boto3 or contacts AWS.
AWS-backed capabilities are declared as Protocols in `ports` and implemented
locally in `adapters.local`; `adapters.aws` holds inert Phase 1/2 placeholders.
"""

__all__ = ["PhaseNotAuthorizedError"]


class PhaseNotAuthorizedError(RuntimeError):
    """Raised when code reaches a capability that a later phase has not authorized.

    This is deliberately loud. A Phase 0 run that touches an AWS-backed stage must
    fail visibly rather than return a fabricated result.
    """
