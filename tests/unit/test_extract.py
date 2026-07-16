import pytest
from paper_pipeline.errors import PipelineError
from paper_pipeline.extract.chunking import chunk_pages
from paper_pipeline.extract.schema import load_schema, parse_json_object, validate


def test_json_recovery() -> None:
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('text ```json\n{"a": 1}\n``` end') == {"a": 1}
    assert parse_json_object('prefix {"a": 1} suffix') == {"a": 1}
    with pytest.raises(PipelineError):
        parse_json_object("[]")


def test_schema_validation() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    validate({"a": 1}, schema)
    with pytest.raises(PipelineError):
        validate({"a": "x"}, schema)


def test_page_chunking() -> None:
    chunks = chunk_pages(["[PDF_PAGE 1]\nabc", "[PDF_PAGE 2]\ndef"], 22)
    assert chunks == ["[PDF_PAGE 1]\nabc", "[PDF_PAGE 2]\ndef"]


def test_missing_default_schema_falls_back_to_package(tmp_path) -> None:
    schema, digest = load_schema(tmp_path / "schemas" / "catalysis.json")

    assert schema["title"] == "Catalysis paper extraction"
    assert len(digest) == 64
