"""Módulo de validación y cuarentena de la información tabular extraída."""
from app.validation.contracts import CONTRACTS, contract_for_family
from app.validation.models import (
    DataContract,
    FieldSpec,
    FieldType,
    QuarantineCause,
    QuarantineState,
    RecordDisposition,
    RecordValidation,
    StructuralValidation,
    ValidationResult,
)
from app.validation.service import ValidationService

__all__ = (
    "CONTRACTS",
    "DataContract",
    "FieldSpec",
    "FieldType",
    "QuarantineCause",
    "QuarantineState",
    "RecordDisposition",
    "RecordValidation",
    "StructuralValidation",
    "ValidationResult",
    "ValidationService",
    "contract_for_family",
)
