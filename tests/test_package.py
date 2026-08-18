from __future__ import annotations

from importlib.metadata import metadata
from importlib.resources import files

from gh_slate import __author__


def test_package_metadata() -> None:
    project = metadata("gh-slate")

    assert project["Name"] == "gh-slate"
    assert project["Author-email"] == "Nyakku Shigure <sigure.qaq@gmail.com>"
    assert __author__ == "Nyakku Shigure"


def test_typing_marker_is_packaged() -> None:
    assert files("gh_slate").joinpath("py.typed").is_file()
