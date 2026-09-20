import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDA_ROOT = REPO_ROOT / "backend" / "lambda"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

if str(LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(LAMBDA_ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return LAMBDA_ROOT / "changeproof" / "fixtures"


@pytest.fixture(scope="session")
def plan_path(fixtures_dir: Path) -> Path:
    return fixtures_dir / "terraform_plan.json"


@pytest.fixture
def graph():
    from changeproof.adapters.local import load_graph

    return load_graph()


@pytest.fixture
def change_set(plan_path: Path):
    from changeproof.parser import parse_plan_file

    return parse_plan_file(plan_path)


@pytest.fixture
def prediction(change_set, graph):
    from changeproof.predict import predict

    return predict(change_set, graph)


@pytest.fixture
def observation():
    from changeproof.adapters.local import FixtureTelemetrySource

    return FixtureTelemetrySource().collect("test")
