FROM mcr.microsoft.com/playwright/python:v1.62.0-noble
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY start-server.sh ./
COPY dashboard_assets ./dashboard_assets
RUN mkdir -p /var/data && chown -R pwuser:pwuser /app /var/data
RUN chmod 755 /app/start-server.sh
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=10000 FM_STORAGE_ROOT=/var/data
EXPOSE 10000
CMD ["/app/start-server.sh"]
