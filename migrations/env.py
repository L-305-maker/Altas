"""迁移连接从环境读取，不把含密码的 URL 写入 alembic.ini 或日志。"""

from alembic import context
from sqlalchemy import create_engine

from agentflow.config import Settings
from agentflow.infrastructure.persistence.models import Base

url = Settings().database_url.get_secret_value()

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, compare_type=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
