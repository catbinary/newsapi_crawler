FROM ubuntu:18.04

RUN apt update && apt install -y fish \
    python-pip \
    python3-pip \
    python3.7 \
    python3-dev \
    g++ \
    postgresql-server-dev-all \
    libpq-dev \
    && apt clean all

RUN mkdir -p /app \
|| mkdir -p /dags \
|| mkdir -p /entrypoint \
|| mkdir -p /keys

COPY ./requirements.txt /app/requirements.txt

WORKDIR /app
RUN pip3 install --upgrade pip
RUN pip3 install -r requirements.txt

ENV AIRFLOW_HOME /app/airflow

COPY ./entrypoint/ /entrypoint/
RUN chmod +x /entrypoint/*

ENTRYPOINT /entrypoint/web.sh