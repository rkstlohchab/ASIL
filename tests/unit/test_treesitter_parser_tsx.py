from __future__ import annotations

from textwrap import dedent

from asil_ingest import (
    ParsedFile,
    SourceLanguage,
    parse_source,
)


def _parse(src: str, *, path: str = "<inline>.tsx", module_name: str | None = None) -> ParsedFile:
    return parse_source(
        dedent(src).lstrip("\n"),
        SourceLanguage.tsx,
        path=path,
        module_name=module_name,
    )


def test_extracts_function_component_with_jsx() -> None:
    pf = _parse(
        """
        import React from 'react';

        function Button(props: { label: string }) {
            return <button onClick={() => alert(props.label)}>{props.label}</button>;
        }
        """,
        module_name="src.components.Button",
    )
    # The host function should be captured even though the body contains JSX.
    fn_names = [fn.name for fn in pf.functions]
    assert "Button" in fn_names
    btn = next(fn for fn in pf.functions if fn.name == "Button")
    assert btn.qualified_name == "src.components.Button.Button"
    # JSX onClick handler is an anonymous arrow inside the body — it's not
    # captured as a top-level function (we only capture *named* arrows), but
    # the call to `alert` inside it should appear in the host's call list.
    callees = [c.callee for c in btn.calls]
    assert "alert" in callees


def test_extracts_arrow_function_component() -> None:
    pf = _parse(
        """
        const Card = ({ title }: { title: string }) => (
            <div className="card"><h2>{title}</h2></div>
        );
        """,
        module_name="src.components.Card",
    )
    fn_names = [fn.name for fn in pf.functions]
    assert "Card" in fn_names


def test_handles_react_class_component() -> None:
    pf = _parse(
        """
        import React from 'react';

        class App extends React.Component {
            render() {
                return <div>hello</div>;
            }
        }
        """,
        module_name="src.App",
    )
    assert len(pf.classes) == 1
    cls = pf.classes[0]
    assert cls.name == "App"
    method_names = [m.name for m in cls.methods]
    assert "render" in method_names


def test_records_parse_errors_without_crashing() -> None:
    pf = _parse(
        """
        const Broken = () => <div<<<
        """
    )
    assert pf.parse_errors
    assert pf.language is SourceLanguage.tsx
