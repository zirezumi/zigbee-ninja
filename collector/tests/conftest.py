import pytest
from fastapi.testclient import TestClient

from zigbee_ninja.api.app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(data_dir=tmp_path)
    with TestClient(app) as test_client:
        yield test_client
