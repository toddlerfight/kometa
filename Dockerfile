FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kometa/ kometa/

EXPOSE 6969

CMD ["uvicorn", "kometa.main:app", "--host", "0.0.0.0", "--port", "6969"]
