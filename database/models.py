# database/models.py
import datetime
from sqlalchemy import BigInteger, String, Text, TIMESTAMP, ForeignKey, UniqueConstraint
from sqlalchemy.types import TypeDecorator, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

class TZDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            raise ValueError("TZDateTime requires timezone-aware datetimes")
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value

class Base(DeclarativeBase):
    pass

class RiotAccount(Base):
    __tablename__ = "riot_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # DiscordのユーザーID
    discord_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # ユーザーが設定するアカウントの別名
    account_name: Mapped[str] = mapped_column(String(50))
    # Riot ID (e.g., "username#tag")
    riot_id: Mapped[str] = mapped_column(String(100))
    
    # Riotアカウント情報
    encrypted_cookies: Mapped[str] = mapped_column(Text)
    auth_token: Mapped[str] = mapped_column(Text)
    entitlement_token: Mapped[str] = mapped_column(Text)
    puuid: Mapped[str] = mapped_column(String(128), unique=True)
    shard: Mapped[str] = mapped_column(String(10), default="ap")
    
    created_at: Mapped[datetime.datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )

    __table_args__ = (
        # discord_user_id と account_name の組み合わせはユニークでなければならない
        UniqueConstraint('discord_user_id', 'account_name', name='_discord_user_account_name_uc'),
    )

    def __repr__(self) -> str:
        return f"<RiotAccount(id={self.id}, discord_user_id={self.discord_user_id}, name='{self.account_name}')>"


class State(Base):
    __tablename__ = "states"

    state_token: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    expiry: Mapped[datetime.datetime] = mapped_column(TZDateTime)

    def __repr__(self) -> str:
        return f"<State(user_id={self.user_id}, expiry={self.expiry})>"


class DailyStoreSchedule(Base):
    __tablename__ = "daily_store_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    riot_account_id: Mapped[int] = mapped_column(ForeignKey("riot_accounts.id", ondelete="CASCADE"), index=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    schedule_time: Mapped[datetime.time] = mapped_column() # HH:MM in UTC

    __table_args__ = (
        # 同じアカウント、同じギルド、同じチャンネルに複数のスケジュールは設定できない
        UniqueConstraint('riot_account_id', 'guild_id', 'channel_id', name='_schedule_uc'),
    )

    def __repr__(self) -> str:
        return f"<DailyStoreSchedule(id={self.id}, user_id={self.discord_user_id}, channel_id={self.channel_id}, time={self.schedule_time})>"