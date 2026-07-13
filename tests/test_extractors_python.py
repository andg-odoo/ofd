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


def test_new_dunder_method_filtered_at_extraction():
    """Adding `__repr__` / `__eq__` / `__set_name__` etc. to a class is
    plumbing, not new API. The extractor used to emit them as
    NEW_DECORATOR_OR_HELPER primitives that piled up as zero-rollout
    ledger noise; now they're suppressed. `__init__` and `__new__`
    are explicitly NOT filtered - those reshape the construction
    surface and their kwarg changes are real API."""
    parent = '''\
class Origin:
    pass
'''
    child = '''\
class Origin:
    def __repr__(self): return ""
    def __eq__(self, other): return False
    def __hash__(self): return 0
    def __set_name__(self, owner, name): pass
    def search(self, domain): pass
'''
    records = extract(parent, child, "odoo/orm/x.py")
    helpers = [r for r in records if r.kind == Kind.NEW_DECORATOR_OR_HELPER]
    symbols = {r.symbol for r in helpers}
    # Only the non-dunder method comes through.
    assert symbols == {"odoo.orm.x.Origin.search"}


def test_new_init_dunder_still_emitted():
    """`__init__` and `__new__` are real construction-surface changes -
    keep them visible at the definition layer."""
    parent = '''\
class Thing:
    pass
'''
    child = '''\
class Thing:
    def __init__(self, foo): pass
    def __new__(cls, *a, **kw): return super().__new__(cls)
'''
    records = extract(parent, child, "odoo/orm/x.py")
    helpers = [r for r in records if r.kind == Kind.NEW_DECORATOR_OR_HELPER]
    symbols = {r.symbol for r in helpers}
    assert symbols == {"odoo.orm.x.Thing.__init__", "odoo.orm.x.Thing.__new__"}


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


def test_new_annotated_class_attribute():
    """Annotated class attrs (`x: int = 1`) were invisible pre-AnnAssign
    support; outside fields modules they emit NEW_CLASS_ATTRIBUTE."""
    parent = "class Registry:\n    pass\n"
    child = "class Registry:\n    ready: bool = False\n"
    records = extract(parent, child, "odoo/modules/registry.py")
    attrs = [r for r in records if r.kind == Kind.NEW_CLASS_ATTRIBUTE]
    assert len(attrs) == 1
    assert attrs[0].symbol == "odoo.modules.registry.Registry.ready"
    assert attrs[0].after_snippet == "    ready: bool = False"


def test_new_field_attribute_is_field_kwarg():
    """`init_storage: ... = None` on Field (odoo#256914). Field attrs
    double as constructor kwargs, so the record is a NEW_KWARG with the
    `__init__` symbol shape the constructor-call rollout matcher keys on."""
    parent = '''\
class Field:
    compute_sudo: bool = True
'''
    child = '''\
class Field:
    compute_sudo: bool = True
    init_storage: str | None = None  # initialize field values
'''
    records = extract(parent, child, "odoo/orm/fields.py")
    kwargs = [r for r in records if r.kind == Kind.NEW_KWARG]
    assert len(kwargs) == 1
    assert kwargs[0].symbol == "odoo.orm.fields.Field.__init__.init_storage"
    assert kwargs[0].signature == "init_storage: str | None = None"
    assert not [r for r in records if r.kind == Kind.NEW_CLASS_ATTRIBUTE]


def test_field_kwarg_on_subclass_and_plain_assign():
    """The promotion covers every fields module layout and plain Assign
    attrs too (`sanitize = True` on Html)."""
    parent = "class Html(_String):\n    pass\n"
    child = "class Html(_String):\n    sanitize = True\n"
    records = extract(parent, child, "odoo/orm/fields_textual.py")
    kwargs = [r for r in records if r.kind == Kind.NEW_KWARG]
    assert len(kwargs) == 1
    assert kwargs[0].symbol == "odoo.orm.fields_textual.Html.__init__.sanitize"


def test_annotation_conversion_is_not_new():
    """`x = 1` -> `x: int = 1` is the same attribute; the orm typing
    sweep must not read as new API."""
    parent = "class Field:\n    store = True\n"
    child = "class Field:\n    store: bool = True\n"
    assert extract(parent, child, "odoo/orm/fields.py") == []


def test_removed_annotated_attribute():
    parent = "class Field:\n    group_operator: str | None = None\n"
    child = "class Field:\n    pass\n"
    records = extract(parent, child, "odoo/orm/fields.py")
    removed = [r for r in records if r.kind == Kind.REMOVED_PUBLIC_SYMBOL]
    assert len(removed) == 1
    assert removed[0].symbol == "odoo.orm.fields.Field.group_operator"


def test_deprecated_decorator_captured():
    """The `@deprecated(...)` decorator form (odoo#256914 deprecated
    Model._init_column this way; warnings.warn never fires for it)."""
    parent = '''\
class Model:
    def _init_column(self, name):
        pass
'''
    child = '''\
class Model:
    @deprecated("Since 20.0, field initialization is defined in Field.init_storage")
    def _init_column(self, name):
        pass
'''
    records = extract(parent, child, "odoo/orm/models.py")
    deps = [r for r in records if r.kind == Kind.DEPRECATION_WARNING_ADDED]
    assert len(deps) == 1
    assert deps[0].warning_text.startswith("Since 20.0")
    assert deps[0].removal_version is None
