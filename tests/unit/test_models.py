from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.enums import AcquisitionStatus, ResourceMode
from app.core.models import Lease, ResourceClaim, ResourceVector

UTC = timezone.utc


def test_resource_vector_rejects_negative_capacity() -> None:
    with pytest.raises(ValidationError):
        ResourceVector(memory_mb=-1)


def test_resource_vector_requires_non_zero_declared_demand() -> None:
    with pytest.raises(ValidationError):
        ResourceVector()


@pytest.mark.parametrize("removed_field", ["allow_pending", "labels"])
def test_claim_rejects_fields_removed_from_v1(removed_field: str) -> None:
    values = {
        "request_id": uuid4(),
        "client_id": "ocr",
        "mode": ResourceMode.SHARED,
        "resources": ResourceVector(memory_mb=1),
        "ttl_seconds": 30,
        removed_field: {} if removed_field == "labels" else True,
    }

    with pytest.raises(ValidationError):
        ResourceClaim.model_validate(values)


def test_acquisition_has_exactly_two_outcomes() -> None:
    assert {status.value for status in AcquisitionStatus} == {"GRANTED", "BUSY"}


def test_lease_requires_timezone_aware_ordered_timestamps() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        Lease(
            lease_id=uuid4(),
            request_id=uuid4(),
            client_id="ocr",
            mode=ResourceMode.SHARED,
            resources=ResourceVector(memory_mb=1),
            granted_at=now,
            expires_at=now - timedelta(seconds=1),
        )
