#!/usr/bin/env sh

BACKUP_DIR="/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/pg_backup_${COMPOSE_PROJECT_NAME}_${TIMESTAMP}.sql.gz"

# Создаем бэкап
pg_dump -U ${POSTGRES_USER} ${POSTGRES_DB} | gzip > ${BACKUP_FILE}

if [ $? -eq 0 ]; then
    echo "Backup created successfully: ${BACKUP_FILE}"
    if [[ -z "${WEBDAV_LOGIN}" ]]; then
        echo "webdav is not configured"
    else
        curl -T ${BACKUP_FILE} -u ${WEBDAV_LOGIN}:${WEBDAV_PASSWORD} ${WEBDAV_URL}
        echo "database backup '${BACKUP_FILE}' has been sent to webdav"
    fi
else
    echo "Error creating backup"
    exit 1
fi 