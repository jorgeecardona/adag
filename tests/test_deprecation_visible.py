"""Deprecation warnings reach a script or notebook user without changing warning filters."""

import subprocess
import sys
from pathlib import Path

from adag import AdagDeprecationWarning


def test_warning_is_shown_by_default_from_an_imported_module(tmp_path: Path) -> None:
    """Python hides a DeprecationWarning raised inside a library; this one is shown."""
    # The deprecated call sits in an imported helper module, as in most scripts and notebooks;
    # a plain DeprecationWarning attributed to that module would be filtered out.
    tmp_path.joinpath("old_helpers.py").write_text(
        "from adag import Parameter\n\ndef make():\n    return Parameter.of('x', int)\n"
    )
    script = tmp_path.joinpath("old_code.py")
    script.write_text("import old_helpers\nold_helpers.make()\n")
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "AdagDeprecationWarning" in result.stderr
    assert "removed in adag 1.0" in result.stderr
    assert "InputParam" in result.stderr


def test_warning_class_is_a_future_warning() -> None:
    assert issubclass(AdagDeprecationWarning, FutureWarning)
    assert not issubclass(AdagDeprecationWarning, DeprecationWarning)
