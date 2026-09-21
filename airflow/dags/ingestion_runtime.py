"""Programa la API de ingesta existente sin reproducir las reglas del dominio."""
from __future__ import annotations

import os

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator
from pendulum import datetime


def run_controlled_ingestion() -> None:
    endpoint = os.getenv("INGESTION_API_URL", "http://backend:8000")
    username = os.environ["AIRFLOW_INGESTION_USERNAME"]
    password = os.environ["AIRFLOW_INGESTION_PASSWORD"]
    login = requests.post(f"{endpoint}/api/auth/login", json={"username": username, "password": password}, timeout=30)
    login.raise_for_status()
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    try:
        for location in ("contracts-documents", "litigation", "compliance", "internal-audit"):
            response = requests.post(f"{endpoint}/api/ingestion/runs", json={"controlled_location": location}, headers=headers, timeout=120)
            response.raise_for_status()
    finally:
        requests.post(f"{endpoint}/api/auth/logout", headers=headers, timeout=30)


with DAG("controlled_ingestion", start_date=datetime(2026, 1, 1), schedule="@daily", catchup=False) as dag:
    PythonOperator(task_id="run_controlled_ingestion", python_callable=run_controlled_ingestion, retries=1)
