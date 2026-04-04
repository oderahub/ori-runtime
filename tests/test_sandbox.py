# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

import sys
import textwrap

import pytest

from ori.skills.sandbox import (
    RestrictedImportFinder,
    SkillSecurityError,
    load_hooks_restricted,
)


def _write_hooks(tmp_path, source: str) -> str:
    hooks_file = tmp_path / "hooks.py"
    hooks_file.write_text(textwrap.dedent(source), encoding="utf-8")
    return str(hooks_file)


def test_allowed_import_math_loads(tmp_path):
    path = _write_hooks(tmp_path, """\
        import math
        result = math.sqrt(16)
    """)
    module = load_hooks_restricted(path)
    assert module is not None
    assert module.result == 4.0


def test_blocked_import_os_raises(tmp_path):
    path = _write_hooks(tmp_path, """\
        import os
    """)
    with pytest.raises(SkillSecurityError):
        load_hooks_restricted(path)


def test_blocked_import_subprocess_raises(tmp_path):
    path = _write_hooks(tmp_path, """\
        import subprocess
    """)
    with pytest.raises(SkillSecurityError):
        load_hooks_restricted(path)


def test_blocked_builtin_open_raises(tmp_path):
    path = _write_hooks(tmp_path, """\
        data = open("/etc/passwd", "r")
    """)
    with pytest.raises((NameError, SkillSecurityError)):
        load_hooks_restricted(path)


def test_nonexistent_path_returns_none():
    result = load_hooks_restricted("/nonexistent/path/hooks.py")
    assert result is None


def test_meta_path_not_polluted_after_success(tmp_path):
    """RestrictedImportFinder must be removed even on successful load."""
    before = list(sys.meta_path)
    path = _write_hooks(tmp_path, "import math\n")
    load_hooks_restricted(path)
    after = list(sys.meta_path)
    assert not any(isinstance(f, RestrictedImportFinder) for f in after)
    assert len(before) == len(after)


def test_meta_path_not_polluted_after_failure(tmp_path):
    """RestrictedImportFinder must be removed even when loading fails."""
    path = _write_hooks(tmp_path, "import os\n")
    with pytest.raises(SkillSecurityError):
        load_hooks_restricted(path)
    assert not any(isinstance(f, RestrictedImportFinder) for f in sys.meta_path)


def test_meta_path_not_polluted_after_runtime_error(tmp_path):
    """RestrictedImportFinder must be removed when exec raises a non-import error.

    This covers the case where the module passes the import check but then
    crashes mid-execution (e.g. a ValueError, ZeroDivisionError, etc.).
    The finder must still be cleaned up even though the failure is not an
    ImportError caught by the SkillSecurityError path.
    """
    path = _write_hooks(tmp_path, """\
        import math          # allowed — clears the import gate
        x = 1 / 0            # raises ZeroDivisionError during exec
    """)
    with pytest.raises(ZeroDivisionError):
        load_hooks_restricted(path)
    assert not any(isinstance(f, RestrictedImportFinder) for f in sys.meta_path)


def test_meta_path_not_polluted_after_nonexistent():
    """No finder should be installed when the file does not exist."""
    before_count = len(sys.meta_path)
    load_hooks_restricted("/no/such/file.py")
    assert len(sys.meta_path) == before_count
    assert not any(isinstance(f, RestrictedImportFinder) for f in sys.meta_path)
