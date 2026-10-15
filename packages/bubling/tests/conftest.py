import pytest
from provider_fakes import ProviderService


@pytest.fixture
def provider_service() -> ProviderService:
    return ProviderService()
