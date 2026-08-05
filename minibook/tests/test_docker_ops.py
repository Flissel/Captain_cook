from pathlib import Path

from minibook.swarm.docker_ops import _compose_project_name


def test_compose_project_name_is_safe_for_docker_image_tags() -> None:
    assert _compose_project_name(Path("C:/Temp/autogen_eval_uuwusrb_")) == (
        "autogen-autogen-eval-uuwusrb"
    )


def test_compose_project_name_uses_a_non_empty_fallback() -> None:
    assert _compose_project_name(Path("C:/Temp/___")) == "autogen-build"
