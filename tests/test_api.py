import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///./test_content_factory.db"
os.environ["OUTPUT_DIR"] = tempfile.mkdtemp(prefix="yt_factory_test_")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


def setup_function() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_content_workflow() -> None:
    client = TestClient(app)

    channel_response = client.post("/channels", json={"name": "Test Channel"})
    assert channel_response.status_code == 200
    channel_id = channel_response.json()["id"]

    video_response = client.post(
        "/videos",
        json={
            "channel_id": channel_id,
            "title": "I Built an AI Receptionist for a Roofing Company",
            "thumbnail_text": "AI ROOFING RECEPTIONIST",
        },
    )
    assert video_response.status_code == 200
    video_id = video_response.json()["id"]

    generated_response = client.post(f"/videos/{video_id}/generate", json={"stage": "all"})
    assert generated_response.status_code == 200
    assert len(generated_response.json()) >= 5

    blocked_package = client.post(f"/videos/{video_id}/package")
    assert blocked_package.status_code == 409

    review_response = client.post(f"/videos/{video_id}/review", json={"passed": True, "notes": "Looks safe"})
    assert review_response.status_code == 200
    assert review_response.json()["passed"] is True

    package_response = client.post(f"/videos/{video_id}/package")
    assert package_response.status_code == 200
    assert "package_dir" in package_response.json()

    payload_response = client.post(f"/publish/{video_id}/prepare-youtube-payload")
    assert payload_response.status_code == 200
    assert payload_response.json()["privacy_status"] == "private"
