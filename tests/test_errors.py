from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from bub.errors import BubError, ErrorKind


def test_error_survives_generator_context_manager_with_identity_and_details() -> None:
    @contextmanager
    def boundary() -> Iterator[None]:
        yield

    error = BubError(ErrorKind.NOT_FOUND, "missing", {"handle": "unknown"})
    with pytest.raises(BubError) as caught, boundary():
        raise error

    assert caught.value is error
    assert caught.value.as_dict() == {"kind": "not_found", "message": "missing", "details": {"handle": "unknown"}}
