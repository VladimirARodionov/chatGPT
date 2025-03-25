#!/usr/bin/env sh

if [ -z "$1" ]; then
    echo "Usage: $0 <backup_file>"
    exit 1
fi

BACKUP_FILE="/backups/$1"

if [ ! -f "${BACKUP_FILE}" ]; then
    echo "Backup file not found: ${BACKUP_FILE}"
    exit 1
fi

before_restore_filename="/backups/before_restore_${POSTGRES_DB}_$(date +'%Y_%m_%dT%H_%M_%S').sql.gz"
pg_dump -U ${POSTGRES_USER} ${POSTGRES_DB} | gzip > "${before_restore_filename}"

echo "database backup '${before_restore_filename}' has been created"

# Отключаем все подключения к базе данных
echo "Disconnecting all users from database..."
psql -U ${POSTGRES_USER} -d postgres -c "
SELECT pg_terminate_backend(pid) 
FROM pg_stat_activity 
WHERE datname = '${POSTGRES_DB}' 
  AND pid <> pg_backend_pid();"

# Восстанавливаем из бэкапа
echo "Dropping existing database..."
dropdb -U ${POSTGRES_USER} ${POSTGRES_DB} --if-exists
createdb -U ${POSTGRES_USER} ${POSTGRES_DB}

echo "Restoring from backup..."
gunzip -c ${BACKUP_FILE} | psql -U ${POSTGRES_USER} ${POSTGRES_DB}

if [ $? -eq 0 ]; then
    echo "Restore completed successfully"
else
    echo "Error during restore"
    exit 1
fi 