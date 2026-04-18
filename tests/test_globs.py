from ofd.globs import match, match_any


def test_exact_match():
    assert match("odoo/fields.py", "odoo/fields.py")
    assert not match("odoo/fields.py", "odoo/api.py")


def test_single_star_in_segment():
    assert match("odoo/models/base.py", "odoo/models/*.py")
    assert not match("odoo/models/base.py", "odoo/api/*.py")
    # * doesn't cross segment boundaries
    assert not match("odoo/models/sub/deep.py", "odoo/models/*.py")


def test_double_star_matches_multiple_segments():
    assert match("odoo/models/base.py", "odoo/models/**/*.py")
    assert match("odoo/models/sub/deep/thing.py", "odoo/models/**/*.py")
    # ** can also match zero segments.
    assert match("odoo/models/thing.py", "odoo/models/**/*.py")


def test_double_star_at_start():
    assert match("odoo/core/foo.py", "**/core/**")
    assert match("x/core/y", "**/core/**")


def test_rng_suffix():
    assert match("odoo/addons/base/rng/view.rng", "odoo/addons/base/rng/*.rng")
    assert not match("odoo/addons/base/rng/sub/view.rng", "odoo/addons/base/rng/*.rng")


def test_match_any_short_circuits():
    patterns = ["odoo/fields.py", "odoo/models/**/*.py"]
    assert match_any("odoo/fields.py", patterns)
    assert match_any("odoo/models/sub/x.py", patterns)
    assert not match_any("addons/sale/foo.py", patterns)


def test_addons_web_static_src_views():
    assert match(
        "odoo/addons/web/static/src/views/kanban/kanban_view.js",
        "odoo/addons/web/static/src/views/**",
    )
