# Railway daily-refresh image.
# Mac uses .venv (gitignored); Railway uses the system Python that pip-installs
# pyproject.toml's dependencies. Both run ops/refresh_local.sh as the entrypoint.
FROM python:3.12-slim

# git + gh + duckdb runtime deps. gh's apt repo signed-by setup is the
# canonical install from cli.github.com.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching when only code changes).
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . \
 && cd /tmp && rm -rf /root/.cache

# Now copy the rest of the repo. .dockerignore strips .venv, node_modules,
# .git, web/public (regenerated), and data/ (on the volume).
COPY . .

RUN chmod +x ops/refresh_local.sh

# Cron entrypoint. Railway will run this on the schedule defined in
# railway.json (or the service's Cron Schedule field).
CMD ["bash", "ops/refresh_local.sh"]
