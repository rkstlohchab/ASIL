"""Unit tests for the call-edge resolver heuristics.

The interesting logic is in `_resolve_one` — given a callee text plus the
context (parent class, file's module prefix, file's imports), return the
fully-qualified name that the call refers to, or None. Tested in isolation
so we don't need a Neo4j round-trip per assertion.
"""

from __future__ import annotations

from asil_ingest.call_resolver import _FileImports, _index_imports, _resolve_one


def _imports(*entries: dict) -> _FileImports:
    return _index_imports(list(entries))


# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------


def test_exact_qname_matches_directly() -> None:
    index = {"pkg.mod.foo"}
    resolved, strategy = _resolve_one(
        callee_text="pkg.mod.foo",
        module_prefix="pkg.other",
        parent_class=None,
        imports=_imports(),
        function_index=index,
    )
    assert resolved == "pkg.mod.foo"
    assert strategy == "exact"


def test_self_method_resolves_to_parent_class_method() -> None:
    index = {"pkg.mod.Service.send"}
    resolved, strategy = _resolve_one(
        callee_text="self.send",
        module_prefix="pkg.mod",
        parent_class="pkg.mod.Service",
        imports=_imports(),
        function_index=index,
    )
    assert resolved == "pkg.mod.Service.send"
    assert strategy == "self_method"


def test_cls_method_uses_same_strategy() -> None:
    index = {"pkg.mod.Service.factory"}
    resolved, strategy = _resolve_one(
        callee_text="cls.factory",
        module_prefix="pkg.mod",
        parent_class="pkg.mod.Service",
        imports=_imports(),
        function_index=index,
    )
    assert resolved == "pkg.mod.Service.factory"
    assert strategy == "self_method"


def test_same_module_bare_name_resolves_against_module_prefix() -> None:
    index = {"pkg.mod.helper"}
    resolved, strategy = _resolve_one(
        callee_text="helper",
        module_prefix="pkg.mod",
        parent_class=None,
        imports=_imports(),
        function_index=index,
    )
    assert resolved == "pkg.mod.helper"
    assert strategy == "same_module"


def test_import_alias_resolves_module_alias_then_attribute() -> None:
    # `import json as j` -> "j" is an alias for "json"
    imports = _imports({"module": "json", "names": [], "alias_of": {"j": "json"}})
    index = {"json.dumps"}
    resolved, strategy = _resolve_one(
        callee_text="j.dumps",
        module_prefix="pkg.mod",
        parent_class=None,
        imports=imports,
        function_index=index,
    )
    assert resolved == "json.dumps"
    assert strategy == "import_alias"


def test_import_member_resolves_from_import_as() -> None:
    # `from typing import Optional as Opt` -> "Opt" is a member alias
    imports = _imports({"module": "typing", "names": ["Any"], "alias_of": {"Opt": "Optional"}})
    index = {"typing.Optional"}
    resolved, strategy = _resolve_one(
        callee_text="Opt",
        module_prefix="pkg.mod",
        parent_class=None,
        imports=imports,
        function_index=index,
    )
    assert resolved == "typing.Optional"
    assert strategy == "import_member"


def test_unresolved_callee_returns_none() -> None:
    resolved, strategy = _resolve_one(
        callee_text="something_we_dont_know",
        module_prefix="pkg.mod",
        parent_class=None,
        imports=_imports(),
        function_index=set(),
    )
    assert resolved is None
    assert strategy == "<unresolved>"


def test_self_method_falls_through_when_method_isnt_in_index() -> None:
    # `self.broken` with no matching qname in the index → falls through to
    # other strategies and eventually unresolved.
    resolved, strategy = _resolve_one(
        callee_text="self.broken",
        module_prefix="pkg.mod",
        parent_class="pkg.mod.Service",
        imports=_imports(),
        function_index={"pkg.mod.Service.other"},
    )
    assert resolved is None
    assert strategy == "<unresolved>"


def test_exact_qname_wins_over_other_strategies() -> None:
    """If the callee text is already a fully-qualified name we know about,
    no further interpretation should run."""
    imports = _imports({"module": "fake", "names": ["pkg"]})
    index = {"pkg.thing", "fake.pkg"}
    resolved, strategy = _resolve_one(
        callee_text="pkg.thing",
        module_prefix="elsewhere",
        parent_class=None,
        imports=imports,
        function_index=index,
    )
    assert resolved == "pkg.thing"
    assert strategy == "exact"


# ---------------------------------------------------------------------------
# import indexing
# ---------------------------------------------------------------------------


def test_index_imports_handles_plain_import() -> None:
    fi = _imports({"module": "json", "names": [], "alias_of": {}})
    assert fi.aliases["json"] == "json"


def test_index_imports_handles_aliased_import() -> None:
    fi = _imports({"module": "numpy", "names": [], "alias_of": {"np": "numpy"}})
    assert fi.aliases["np"] == "numpy"


def test_index_imports_handles_from_import_with_alias() -> None:
    fi = _imports(
        {
            "module": "typing",
            "names": ["Any", "Optional"],
            "alias_of": {"Opt": "Optional"},
        }
    )
    assert fi.members["Any"] == "typing.Any"
    assert fi.members["Optional"] == "typing.Optional"
    assert fi.members["Opt"] == "typing.Optional"


def test_index_imports_skips_wildcard() -> None:
    fi = _imports({"module": "foo", "names": ["*"], "alias_of": {}})
    assert "*" not in fi.members
    assert "*" not in fi.aliases
