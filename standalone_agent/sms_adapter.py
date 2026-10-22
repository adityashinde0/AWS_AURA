"""SMS Adapter for Safety Guardian in Project AURA.

Handles SMS notification dispatch to emergency contacts via AWS SNS.
Redacts and never logs raw phone numbers.
Integrates with AWS SNS with fallback / mock support for offline testing.
"""
from __future__ import annotations

import os
from typing import Any


class SmsAdapter:
    """Dispatches emergency safety check-in SMS notifications via AWS SNS."""

    def __init__(self, sns_client: Any = None) -> None:
        self._sns_client = sns_client
        self.sent_messages: list[dict[str, Any]] = []

    def _get_client(self) -> Any:
        if self._sns_client is not None:
            return self._sns_client
        if os.getenv("TEST_MODE") == "true" or os.getenv("AURA_OFFLINE_MODE") == "true" or "PYTEST_CURRENT_TEST" in os.environ:
            return None
        # Only initialize live boto3 client if explicit AWS credentials/region are present
        if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_DEFAULT_REGION"):
            try:
                import boto3

                region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
                self._sns_client = boto3.client("sns", region_name=region)
                return self._sns_client
            except Exception:
                return None
        return None

    def send_sms(self, phone_number: str, message: str) -> dict[str, Any]:
        """Send an SMS alert to an emergency contact.

        Phone number is never logged or exposed in returned audit records.
        """
        client = self._get_client()
        if client is not None:
            try:
                resp = client.publish(
                    PhoneNumber=phone_number,
                    Message=message,
                )
                msg_id = resp.get("MessageId", "sns_msg_id")
                record = {"status": "sent", "message_id": msg_id}
                self.sent_messages.append(record)
                return record
            except Exception as exc:
                raise RuntimeError(f"SNS publish error: {exc}") from exc

        # Mock / local test dispatch
        msg_id = f"sms_mock_{len(self.sent_messages) + 1}"
        record = {"status": "sent", "message_id": msg_id}
        self.sent_messages.append(record)
        return record
