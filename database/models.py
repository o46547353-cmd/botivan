from sqlalchemy import Column, Integer, BigInteger, String, Date, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database.db import Base

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=False)
    ref_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=True)
    subscription_status = Column(String, default="Inactive")
    expire_date = Column(Date, nullable=True)
    tier = Column(Integer, default=0)
    phone_number = Column(String, nullable=True)
    is_banned = Column(Boolean, default=False)
    referral_rewarded = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    
    tickets = relationship("Ticket", back_populates="user", cascade="all, delete-orphan")


class Ticket(Base):
    __tablename__ = 'tickets'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False)
    ticket_number = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, default="Active")
    ticket_type = Column(String, default="regular")
    
    # НОВАЯ СТРОКА: Автоматически запоминаем дату и время создания билета
    created_at = Column(DateTime, default=func.now()) 

    user = relationship("User", back_populates="tickets")


class PaymentConfig(Base):
    __tablename__ = 'payment_configs'

    id = Column(Integer, primary_key=True, index=True)
    tier = Column(Integer, unique=True, nullable=False)
    text = Column(String, nullable=True)
    photo_file_id = Column(String, nullable=True)

class SubscriptionRequest(Base):
    __tablename__ = 'subscription_requests'
    
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, ForeignKey('users.telegram_id'), nullable=False)
    tier = Column(Integer, nullable=False)
    photo_file_id = Column(String, nullable=True)
    status = Column(String, default="Pending")

class ScheduledPost(Base):
    __tablename__ = 'scheduled_posts'

    id = Column(Integer, primary_key=True, index=True)
    text = Column(String, nullable=True)
    media_file_id = Column(String, nullable=True)
    media_type = Column(String, nullable=True) # 'photo', 'video', 'none'
    buttons_json = Column(String, nullable=True) # json representation of list of dicts: [{"text": "...", "url": "..."}]
    publish_at = Column(DateTime, nullable=False, index=True)
    status = Column(String, default="Pending") # 'Pending', 'Sent', 'Failed'
    created_at = Column(DateTime, default=func.now())

class AdminUser(Base):
    __tablename__ = 'admin_users'
    
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, index=True, nullable=True)
    username = Column(String, unique=True, index=True, nullable=True)
    created_at = Column(DateTime, default=func.now())

class BotConfig(Base):
    __tablename__ = 'bot_configs'
    
    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=True)