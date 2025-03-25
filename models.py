from sqlalchemy import Column, Integer, Date, BigInteger
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
