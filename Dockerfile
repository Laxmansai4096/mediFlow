# One image for every service and worker; the Container App's command picks the role.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Microsoft ODBC driver for passwordless Azure SQL (managed identity)
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
 && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql.list \
 && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && apt-get purge -y curl gnupg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-azure.txt ./
RUN pip install -r requirements.txt -r requirements-azure.txt

COPY mediflow ./mediflow
COPY frontend ./frontend

# Run as an unprivileged user
RUN useradd --uid 10001 --no-create-home mediflow && mkdir -p /app/data && chown mediflow /app/data
USER 10001

EXPOSE 8000
CMD ["uvicorn", "mediflow.services.gateway.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
