from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from .models import Base

# データベースファイル名
DB_FILE = "users.db"

# 非同期エンジンを作成
engine = create_async_engine(f"sqlite+aiosqlite:///{DB_FILE}", echo=False)

async_session = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    """データベースのテーブルを初期化（存在しない場合のみ作成）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)