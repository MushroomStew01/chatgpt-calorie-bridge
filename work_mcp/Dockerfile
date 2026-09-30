FROM python:3.12-slim
WORKDIR /adapter
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8023
CMD ["uvicorn", "server:from_environment", "--factory", "--host", "127.0.0.1", "--port", "8023", "--no-access-log"]
