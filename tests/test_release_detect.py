"""Tests for parsing odoo/release.py version_info."""

from ofd.release_detect import detect_version, is_release_file

_RELEASE_PY_19_4 = """\
RELEASE_LEVELS = [ALPHA, BETA, RELEASE_CANDIDATE, FINAL] = ['alpha', 'beta', 'candidate', 'final']

version_info = (19, 4, 0, ALPHA, 1, '')
series = serie = major_version = '.'.join(str(s) for s in version_info[:2])
"""


_RELEASE_PY_SAAS = """\
version_info = ('saas~17', 3, 0, ALPHA, 0, '')
"""


def test_detects_numeric_major():
    assert detect_version(_RELEASE_PY_19_4) == "19.4"


def test_detects_saas_major():
    assert detect_version(_RELEASE_PY_SAAS) == "saas~17.3"


def test_returns_none_on_empty():
    assert detect_version(None) is None
    assert detect_version("") is None


def test_returns_none_on_unknown_shape():
    assert detect_version("version_info = 'not a tuple'") is None


def test_is_release_file_matches_only_known_paths():
    assert is_release_file("odoo/release.py")
    assert not is_release_file("odoo/fields.py")
    assert not is_release_file("release.py")
