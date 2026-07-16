from pathlib import Path

from paper_pipeline.cli import app
from typer.testing import CliRunner


def test_init_uses_installed_package_resource(tmp_path: Path) -> None:
    config = tmp_path / "pipeline.toml"
    result = CliRunner().invoke(app, ["init", "--config", str(config)])

    assert result.exit_code == 0, result.output
    assert config.exists()
    assert "database_url" in config.read_text(encoding="utf-8")
    assert (tmp_path / "data" / "papers.db").exists()
    assert (tmp_path / "schemas" / "catalysis.json").exists()
    assert (tmp_path / "schemas" / "generic.json").exists()
