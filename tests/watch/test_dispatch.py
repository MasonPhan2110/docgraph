from pathlib import Path

from docgraph.config import Config
from docgraph.ingest.lang_dispatch import detect_materialize

_DATA_DIR = Path("/tmp")


def test_native_text_extensions():
    cfg = Config(data_dir=_DATA_DIR)
    for ext in (".md", ".py", ".rs", ".js", ".json", ".yaml", ".sh"):
        assert detect_materialize(Path(f"x{ext}"), cfg) is False, ext


def test_binary_convert_extensions():
    cfg = Config(data_dir=_DATA_DIR)
    for ext in (".pdf", ".docx", ".pptx", ".xlsx", ".epub"):
        assert detect_materialize(Path(f"x{ext}"), cfg) is True, ext


def test_unsupported_returns_none():
    cfg = Config(data_dir=_DATA_DIR)
    for ext in (".dmg", ".iso", ".bin", ".exe", ".png"):
        assert detect_materialize(Path(f"x{ext}"), cfg) is None, ext


def test_case_insensitive():
    cfg = Config(data_dir=_DATA_DIR)
    assert detect_materialize(Path("README.MD"), cfg) is False
    assert detect_materialize(Path("Doc.PDF"), cfg) is True


def test_extra_text_exts_extends():
    cfg = Config(data_dir=_DATA_DIR)
    cfg.watch_extra_text_exts = [".tf", ".hcl"]
    assert detect_materialize(Path("main.tf"), cfg) is False
    assert detect_materialize(Path("vars.hcl"), cfg) is False


def test_extra_binary_exts_extends():
    cfg = Config(data_dir=_DATA_DIR)
    cfg.watch_extra_binary_exts = [".keynote"]
    assert detect_materialize(Path("slide.keynote"), cfg) is True


def test_no_extension_returns_none():
    cfg = Config(data_dir=_DATA_DIR)
    assert detect_materialize(Path("Makefile"), cfg) is None
