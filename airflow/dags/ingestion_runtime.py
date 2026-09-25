"""Programa la API de ingesta y la coordinación de despacho sin reproducir reglas del dominio."""
from __future__ import annotations

import os
from uuid import UUID, uuid5

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator
from pendulum import datetime


ENTRUTABLE_TARGETS = {"VALIDATION", "DOCUMENT"}


_IDENTITY_NAMESPACE = UUID("8a5d92af-79e4-4644-8752-cc02e8f52c4b")


def _correlation_id(context) -> str:
    start = context["data_interval_start"].in_timezone("UTC")
    end = context["data_interval_end"].in_timezone("UTC")
    return str(uuid5(_IDENTITY_NAMESPACE, f"controlled_ingestion:{start.isoformat()}:{end.isoformat()}"))


def _login(endpoint: str) -> tuple[dict[str, str], str]:
    username = os.environ["AIRFLOW_INGESTION_USERNAME"]
    password = os.environ["AIRFLOW_INGESTION_PASSWORD"]
    login = requests.post(f"{endpoint}/api/auth/login", json={"username": username, "password": password}, timeout=30)
    login.raise_for_status()
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, username


def run_controlled_ingestion(**context) -> None:
    endpoint = os.getenv("INGESTION_API_URL", "http://backend:8000")
    # El cliente espera más que el tiempo máximo de procesamiento del backend.
    timeout = int(os.getenv("COORDINATION_TIMEOUT_SECONDS", "360"))
    correlation_id = _correlation_id(context)
    headers, _ = _login(endpoint)
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


def request_scheduled_kpi_recalculation(**context) -> None:
    endpoint = os.getenv("INGESTION_API_URL", "http://backend:8000")
    headers, _ = _login(endpoint)
    try:
        start = context["data_interval_start"].in_timezone("UTC")
        end = context["data_interval_end"].in_timezone("UTC")
        response = requests.post(
            f"{endpoint}/api/coordination/kpi-recalculations/scheduled",
            json={
                "data_interval_start": start.isoformat(),
                "data_interval_end": end.isoformat(),
                "correlation_id": _correlation_id(context),
            },
            headers=headers,
            timeout=int(os.getenv("COORDINATION_TIMEOUT_SECONDS", "360")),
        )
        response.raise_for_status()
    finally:
        requests.post(f"{endpoint}/api/auth/logout", headers=headers, timeout=30)


with DAG("controlled_ingestion", start_date=datetime(2026, 1, 1), schedule="@daily", catchup=False) as dag:
    ingestion = PythonOperator(task_id="run_controlled_ingestion", python_callable=run_controlled_ingestion, retries=1)
    recalculation = PythonOperator(task_id="request_scheduled_kpi_recalculation", python_callable=request_scheduled_kpi_recalculation, retries=1)
    ingestion >> recalculation
