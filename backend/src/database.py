import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker,DeclarativeBase
_url=os.getenv('DATABASE_URL','mysql+pymysql://solar:solar@db:3306/solar')
# 生产按 READ COMMITTED 避免旧快照；SQLite 仅用于本地测试，不支持该隔离级别。
_options={} if _url.startswith('sqlite') else {'isolation_level':'READ COMMITTED'}
engine=create_engine(_url,pool_pre_ping=True,**_options)
Session=sessionmaker(engine,expire_on_commit=False)
class Base(DeclarativeBase):pass
def session():
    with Session() as db:yield db
