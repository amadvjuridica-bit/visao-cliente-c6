FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_SERVER_PORT=8501
ENV C6_DATA_DIR=/data_store

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /data_store

EXPOSE 8501

CMD ["sh", "-c", "if [ ! -f \"$C6_DATA_DIR/cloud_seed_version.json\" ] && [ -d /app/data_store ]; then cp -a /app/data_store/. \"$C6_DATA_DIR\"/; fi; exec streamlit run app.py --server.port=8501 --server.address=0.0.0.0"]
