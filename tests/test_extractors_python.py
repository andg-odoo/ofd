"""Tests for the Python AST-diff extractor.

Covers every emitted Kind using fixture snippets modeled on real Odoo
changes referenced in the user's highlights log (models.CachedModel,
compute_sql= on fields.Boolean, Query.join deprecation, etc.).
"""

from ofd.events.record import Kind
from ofd.extractors.python_ import extract


def test_new_public_class_models_cached():
    """models.CachedModel was introduced in master/19.1+ - canonical case."""
    parent = '''\
"""Cached-model machinery (pre-CachedModel state)."""

class AbstractModel:
    _abstract = True
'''
    child = '''\
"""Cached-model machinery."""

class AbstractModel:
    _abstract = True


class CachedModel(AbstractModel):
    """Model type that caches selected fields."""
    _cached_data_domain = []
    _cached_data_fields = ()
'''
    records = extract(parent, child, "odoo/orm/models_cached.py")
    kinds = [r.kind for r in records]
    assert Kind.NEW_PUBLIC_CLASS in kinds
    cached = next(r for r in records if r.kind == Kind.NEW_PUBLIC_CLASS)
    assert cached.symbol == "odoo.orm.models_cached.CachedModel"
    assert cached.signature == "class CachedModel(AbstractModel)"
    assert "CachedModel" in cached.after_snippet
    assert "_cached_data_fields" in cached.after_snippet


def test_new_public_class_no_bases():
    child = "class Domain:\n    pass\n"
    records = extract(None, child, "odoo/fields.py")
    assert len(records) == 1
    assert records[0].kind == Kind.NEW_PUBLIC_CLASS
    assert records[0].signature == "class Domain"


def test_new_module_level_function_is_helper():
    parent = "x = 1\n"
    child = "x = 1\n\ndef qualify(path, name):\n    return f'{path}.{name}'\n"
    records = extract(parent, child, "odoo/tools/naming.py")
    helpers = [r for r in records if r.kind == Kind.NEW_DECORATOR_OR_HELPER]
    assert len(helpers) == 1
    assert helpers[0].symbol == "odoo.tools.naming.qualify"
    assert helpers[0].signature == "def qualify(path, name)"


def test_new_method_on_existing_class():
    parent = '''\
class Model:
    def search(self, domain):
        pass
'''
    child = '''\
class Model:
    def search(self, domain):
        pass

    def search_count(self, domain):
        pass
'''
    records = extract(parent, child, "odoo/models/base.py")
    helpers = [r for r in records if r.kind == Kind.NEW_DECORATOR_OR_HELPER]
    assert len(helpers) == 1
    assert helpers[0].symbol == "odoo.models.base.Model.search_count"


def test_removed_public_symbol():
    parent = '''\
class Old:
    pass

def helper():
    pass
'''
    child = "def helper():\n    pass\n"
    records = extract(parent, child, "odoo/tools/legacy.py")
    removed = [r for r in records if r.kind == Kind.REMOVED_PUBLIC_SYMBOL]
    assert {r.symbol for r in removed} == {"odoo.tools.legacy.Old"}


def test_signature_change_new_kwarg():
    """compute_sql= landing on a field constructor - exact shape of the
    master change referenced in the highlights."""
    parent = '''\
class Field:
    def __init__(self, string=None, compute=None, search=None):
        pass
'''
    child = '''\
class Field:
    def __init__(self, string=None, compute=None, search=None, compute_sql=None):
        pass
'''
    records = extract(parent, child, "odoo/fields.py")
    sig = [r for r in records if r.kind == Kind.SIGNATURE_CHANGE]
    assert len(sig) == 1
    assert sig[0].symbol == "odoo.fields.Field.__init__"
    assert "compute_sql" in sig[0].after_signature
    assert "compute_sql" not in sig[0].before_signature
    # And the new kwarg is emitted as its own findable primitive.
    kwargs = [r for r in records if r.kind == Kind.NEW_KWARG]
    assert len(kwargs) == 1
    assert kwargs[0].symbol == "odoo.fields.Field.__init__.compute_sql"


def test_new_kwarg_on_free_function():
    parent = "def search(domain, offset=0):\n    pass\n"
    child = "def search(domain, offset=0, limit=None, order=None):\n    pass\n"
    records = extract(parent, child, "odoo/models/base.py")
    kwargs = sorted(
        r.symbol for r in records if r.kind == Kind.NEW_KWARG
    )
    assert kwargs == [
        "odoo.models.base.search.limit",
        "odoo.models.base.search.order",
    ]


def test_new_kwarg_ignores_private_args():
    parent = "def f(a):\n    pass\n"
    child = "def f(a, _private=None, b=1):\n    pass\n"
    records = extract(parent, child, "odoo/m.py")
    kwargs = {r.symbol for r in records if r.kind == Kind.NEW_KWARG}
    assert "odoo.m.f.b" in kwargs
    assert not any("_private" in s for s in kwargs)


def test_new_kwarg_vararg_and_kwarg():
    parent = "def f(a):\n    pass\n"
    child = "def f(a, *args, **kwargs):\n    pass\n"
    records = extract(parent, child, "odoo/m.py")
    kwargs = {r.symbol for r in records if r.kind == Kind.NEW_KWARG}
    assert "odoo.m.f.args" in kwargs
    assert "odoo.m.f.kwargs" in kwargs


def test_no_kwargs_when_signature_unchanged_in_args():
    """Switching positional to keyword-only without adding args -> no NEW_KWARG."""
    parent = "def f(a, b):\n    pass\n"
    child = "def f(a, *, b):\n    pass\n"
    records = extract(parent, child, "odoo/m.py")
    kwargs = [r for r in records if r.kind == Kind.NEW_KWARG]
    # Signature changed (args_hash differs) but no new arg names.
    sig = [r for r in records if r.kind == Kind.SIGNATURE_CHANGE]
    assert sig
    assert kwargs == []


def test_signature_change_ignores_body_only_edits():
    parent = '''\
def do():
    return 1
'''
    child = '''\
def do():
    return 2
'''
    records = extract(parent, child, "odoo/tools/thing.py")
    sig_changes = [r for r in records if r.kind == Kind.SIGNATURE_CHANGE]
    assert sig_changes == []


def test_new_class_attribute():
    parent = "class Domain:\n    pass\n"
    child = "class Domain:\n    TRUE = object()\n    FALSE = object()\n"
    records = extract(parent, child, "odoo/domain.py")
    attrs = [r for r in records if r.kind == Kind.NEW_CLASS_ATTRIBUTE]
    assert {r.symbol for r in attrs} == {"odoo.domain.Domain.TRUE", "odoo.domain.Domain.FALSE"}


def test_deprecation_warning_added_with_removal_version():
    parent = '''\
def AND(domains):
    return domains
'''
    child = '''\
import warnings

def AND(domains):
    warnings.warn(
        "AND() is deprecated, use Domain.AND; removed in 19.1",
        DeprecationWarning,
        stacklevel=2,
    )
    return domains
'''
    records = extract(parent, child, "odoo/osv/expression.py")
    deps = [r for r in records if r.kind == Kind.DEPRECATION_WARNING_ADDED]
    assert len(deps) == 1
    assert "use Domain.AND" in deps[0].warning_text
    assert deps[0].removal_version == "19.1"


def test_deprecation_without_version_still_captured():
    parent = "def foo(): return 1\n"
    child = '''\
import warnings

def foo():
    warnings.warn("foo() is deprecated", DeprecationWarning)
    return 1
'''
    records = extract(parent, child, "odoo/tools/legacy.py")
    deps = [r for r in records if r.kind == Kind.DEPRECATION_WARNING_ADDED]
    assert len(deps) == 1
    assert deps[0].removal_version is None


def test_private_symbols_ignored():
    parent = ""
    child = '''\
def _internal():
    pass

class _Helper:
    pass
'''
    records = extract(parent, child, "odoo/tools/internal.py")
    assert records == []


def test_file_deletion_emits_removals():
    parent = '''\
class Thing:
    pass

def top():
    pass
'''
    records = extract(parent, None, "odoo/gone.py")
    kinds = {r.kind for r in records}
    assert kinds == {Kind.REMOVED_PUBLIC_SYMBOL}
    assert {r.symbol for r in records} == {"odoo.gone.Thing", "odoo.gone.top"}


def test_file_addition_emits_new_symbols():
    child = '''\
class Fresh:
    pass
'''
    records = extract(None, child, "odoo/brand/new.py")
    assert len(records) == 1
    assert records[0].kind == Kind.NEW_PUBLIC_CLASS
    assert records[0].symbol == "odoo.brand.new.Fresh"


def test_qualifier_strips_init():
    child = "class Foo:\n    pass\n"
    records = extract(None, child, "odoo/orm/__init__.py")
    assert records[0].symbol == "odoo.orm.Foo"


def test_snippet_truncation_for_large_class():
    body = "\n".join(f"    line_{i} = {i}" for i in range(80))
    child = f"class Big:\n{body}\n"
    records = extract(None, child, "odoo/big.py")
    assert len(records) == 1
    assert "elided" in records[0].after_snippet


def test_posonly_and_kwonly_signatures():
    parent = '''\
def f(a, b):
    pass
'''
    child = '''\
def f(a, b, /, *, c):
    pass
'''
    records = extract(parent, child, "odoo/m.py")
    sig = [r for r in records if r.kind == Kind.SIGNATURE_CHANGE]
    assert len(sig) == 1
    assert "/" in sig[0].after_signature
    assert "*" in sig[0].after_signature
    assert "c" in sig[0].after_signature
