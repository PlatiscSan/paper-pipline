from pathlib import Path

from paper_pipeline.config import load_settings


def test_env_expansion_and_relative_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BASE", "https://provider.test/v1")
    config = tmp_path / "pipeline.toml"
    config.write_text(
        'database_url="sqlite:///db.sqlite"\npapers_dir="pdfs"\n[extraction.provider]\nbase_url="${BASE}"\n',
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.extraction.provider.base_url == "https://provider.test/v1"
    assert settings.papers_dir == tmp_path / "pdfs"


def test_credentials_load_from_toml_fields(tmp_path: Path) -> None:
    config = tmp_path / "pipeline.toml"
    config.write_text(
        'academic_email="researcher@example.org"\n'
        '[crawler]\nweb_of_science_api_key="wos-key"\n'
        '[downloader]\nsemantic_scholar_api_key="download-key"\n'
        '[extraction.provider]\napi_key="ai-key"\n',
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.academic_email == "researcher@example.org"
    assert settings.crawler.web_of_science_api_key.get_secret_value() == "wos-key"
    assert settings.downloader.semantic_scholar_api_key.get_secret_value() == "download-key"
    assert settings.extraction.provider.api_key.get_secret_value() == "ai-key"
