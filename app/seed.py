from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Channel, Video
from app.services.content_engine import generate_video_ideas


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        channel = db.scalar(select(Channel).where(Channel.name == "Local AI Operator"))
        if channel is None:
            channel = Channel(
                name="Local AI Operator",
                niche="AI automation for local service businesses",
                audience="Roofers, HVAC companies, plumbers, restaurants, med spas, contractors, and local operators",
                brand_voice="Direct, practical, slightly dramatic, not hypey",
                visual_style="Dark premium dashboard style with cream typography and restrained orange/gold accents",
            )
            db.add(channel)
            db.commit()
            db.refresh(channel)

        existing = db.scalars(select(Video).where(Video.channel_id == channel.id)).all()
        if not existing:
            for idea in generate_video_ideas(10):
                db.add(Video(channel_id=channel.id, **idea))
            db.commit()

        print(f"Seeded channel_id={channel.id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
