import logging.config
import os
import pathlib

import sqlalchemy
import decouple

ENVIRONMENT = os.getenv("ENVIRONMENT", default="DEVELOPMENT")

def get_env_config() -> decouple.Config:
    """
    Creates and returns a Config object based on the environment setting.
    It uses .env.dev for development and .env for production.
    """
    env_files = {
        "DEVELOPMENT": ".env.dev",
        "PRODUCTION": ".env",
    }

    app_dir_path = pathlib.Path(__file__).resolve().parent
    env_file_name = env_files.get(ENVIRONMENT, ".env.dev")
    file_path = app_dir_path / env_file_name

    if not file_path.is_file():
        raise FileNotFoundError(f"Environment file not found: {file_path}")

    return decouple.Config(decouple.RepositoryEnv(file_path))

env_config = get_env_config()

if ENVIRONMENT == 'PRODUCTION':
    db_name = env_config.get('POSTGRES_DB')
    db_user = env_config.get('POSTGRES_USER')
    db_pass = env_config.get('POSTGRES_PASSWORD')
    db_host = 'postgres'
    db_port = env_config.get('POSTGRES_PORT') or '5432'
    db_string = 'postgresql://{}:{}@{}:{}/{}'.format(db_user, db_pass, db_host, db_port, db_name)
else:
    db_string = 'sqlite:///local.db'
db = sqlalchemy.create_engine(
    db_string,
    **(
        dict(pool_recycle=900, pool_size=100, max_overflow=3)
    )
)

logging.config.fileConfig(fname=pathlib.Path(__file__).resolve().parent / 'logging.ini',
                          disable_existing_loggers=False)
logging.getLogger('aiogram.dispatcher').propagate = False
logging.getLogger('aiogram.event').propagate = False

superusers = [int(superuser_id) for superuser_id in env_config.get('SUPERUSERS').split(',')]
