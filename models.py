from sqlalchemy import Column, Integer, Date, BigInteger, DateTime, String, Boolean, func, Numeric, Float
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

Base = declarative_base()
metadata = Base.metadata

class UserMessageCount(Base):
    __tablename__ = "user_message_counts"

    user_id = Column(BigInteger, primary_key=True)
    count = Column(Integer, default=0, nullable=False)
    date = Column(Date, nullable=False, default=datetime.now().date)


class TranscribeQueue(Base):
    __tablename__ = "queue"

    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    file_path = Column(String, nullable=False)
    file_name = Column(String, nullable=False)
    file_size_mb = Column(Float)
    message_id = Column(BigInteger, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    is_active = Column(Boolean, default=True)
    finished = Column(Boolean, default=False)
    cancelled = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
