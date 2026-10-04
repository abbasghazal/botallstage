FROM python:3.11-slim-trixie
ARG POSTGRES_CLIENT_MAJOR=18
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Use the official PostgreSQL repository rather than Debian's older default client.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --show-error --location --retry 3 https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && printf 'Types: deb\nURIs: https://apt.postgresql.org/pub/repos/apt\nSuites: trixie-pgdg\nComponents: main\nSigned-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc\n' > /etc/apt/sources.list.d/pgdg.sources \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-${POSTGRES_CLIENT_MAJOR} \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/lib/postgresql/${POSTGRES_CLIENT_MAJOR}/bin:${PATH}"
RUN pg_dump --version && pg_restore --version
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
