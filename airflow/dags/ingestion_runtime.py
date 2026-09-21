"""Programa la API de ingesta y la coordinación de despacho sin reproducir reglas del dominio."""
from __future__ import annotations

import os
from uuid import uuid4

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator
from pendulum import datetime


ENTRUTABLE_TARGETS = {"VALIDATION", "DOCUMENT"}


def run_controlled_ingestion() -> None:
    endpoint = os.getenv("INGESTION_API_URL", "http://backend:8000")
    username = os.environ["AIRFLOW_INGESTION_USERNAME"]
    password = os.environ["AIRFLOW_INGESTION_PASSWORD"]
    # El cliente espera más que el tiempo máximo de procesamiento del backend.
    timeout = int(os.getenv("COORDINATION_TIMEOUT_SECONDS", "360"))
    # Correlación del ciclo completo; solo trazabilidad, no identidad idempotente.
    correlation_id = str(uuid4())

    login = requests.post(f"{endpoint}/api/auth/login", json={"username": username, "password": password}, timeout=30)
    login.raise_for_status()
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    try:
        for location in ("contracts-documents", "litigation", "compliance", "internal-audit"):
            response = requests.post(
                f"{endpoint}/api/ingestion/runs",
                json={"controlled_location": location},
                headers=headers,
                timeout=120,
            )
            response.raise_for_status()
            for result in response.json().get("results", []):
                if result.get("routing_target") not in ENTRUTABLE_TARGETS:
                    continue
                dispatch = requests.post(
                    f"{endpoint}/api/coordination/dispatch",
                    json={"file_id": result["file_id"], "correlation_id": correlation_id},
                    headers=headers,
                    timeout=timeout,
                )
                dispatch.raise_for_status()
    finally:
        requests.post(f"{endpoint}/api/auth/logout", headers=headers, timeout=30)


with DAG("controlled_ingestion", start_date=datetime(2026, 1, 1), schedule="@daily", catchup=False) as dag:
    PythonOperator(task_id="run_controlled_ingestion", python_callable=run_controlled_ingestion, retries=1)
