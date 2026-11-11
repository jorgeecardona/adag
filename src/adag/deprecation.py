"""The warning adag raises for deprecated names and call forms.

``AdagDeprecationWarning`` subclasses ``FutureWarning``, not ``DeprecationWarning``. Python's
default filters hide a ``DeprecationWarning`` raised inside an imported module, so a script or a
notebook cell that calls a deprecated adag name would never see it. A ``FutureWarning`` is shown
by default. Every message names the replacement and the version that removes the old form.

To silence them: ``warnings.filterwarnings("ignore", category=AdagDeprecationWarning)``.
"""

import warnings

REMOVED_IN = "1.0"


class AdagDeprecationWarning(FutureWarning):
    """A deprecated adag name or call form. Shown by default; removed in adag 1.0."""


def warn_deprecated(old: str, replacement: str, *, stacklevel: int = 2) -> None:
    """Warn that ``old`` is deprecated, naming ``replacement`` and the removal version."""
    warnings.warn(
        f"{old} is deprecated and is removed in adag {REMOVED_IN}. Use {replacement} instead.",
        AdagDeprecationWarning,
        stacklevel=stacklevel + 1,
    )
