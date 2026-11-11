from __future__ import annotations

import pytest

import bub.configure as configure
from bub.errors import BubError, ErrorKind


def test_load_invalid_yaml_raises_config_error(write_config) -> None:
    config_file = write_config(":\n\tbad: [unclosed")

    with pytest.raises(BubError) as excinfo:
        configure.load(config_file)

    assert excinfo.value.kind == ErrorKind.CONFIG


def test_load_non_mapping_yaml_raises_config_error(write_config) -> None:
    config_file = write_config("- just\n- a\n- list\n")

    with pytest.raises(BubError) as excinfo:
        configure.load(config_file)

    assert excinfo.value.kind == ErrorKind.CONFIG
