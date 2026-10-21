"""AURA Cloud Calendar Adapter Lambda handler.

Receives calendar webhook payloads from Google Calendar, Outlook, or AURA webhooks,
validates schema, evaluates user consent, and puts MeetingPrepTriggered events
onto the custom EventBridge bus for T-5 preparation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
root_dir = str(Path(__file__).resolve().parent.parent)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from pydantic import ValidationError

from standalone_agent.calendar_scheduler import (
    CalendarEvent,
    CalendarScheduler,
    create_meeting_prep_event,
    normalize_event,
)

scheduler = CalendarScheduler()


def get_eventbridge_client():
    import boto3
    return boto3.client("events", region_name=os.getenv("AWS_REGION", "us-east-1"))


def emit_eventbridge_prep_event(event_dict: dict[str, Any], event_bus_name: str | None = None) -> dict[str, Any]:
    """Emit MeetingPrepTriggered event to custom EventBridge bus."""
    bus_name = event_bus_name or os.getenv("AURA_EVENT_BUS", "aura-event-bus")
    client = get_eventbridge_client()

    response = client.put_events(
        Entries=[
            {
                "EventBusName": bus_name,
                "Source": event_dict["source"],
                "DetailType": event_dict["detail-type"],
                "Detail": json.dumps(event_dict["detail"]),
            }
        ]
    )
    return response


def lambda_handler(event: dict[str, Any], _context: object = None) -> dict[str, Any]:
    """HTTP or direct Lambda handler for calendar event intake."""
    try:
        raw_body = event.get("body", event)
        if isinstance(raw_body, str):
            raw_body = json.loads(raw_body)

        source_type = event.get("headers", {}).get("x-calendar-source", "aura").lower()
        if source_type not in ("google", "outlook", "aura"):
            source_type = "aura"

        user_id = raw_body.get("user_id") or event.get("requestContext", {}).get("authorizer", {}).get("user_id")
        cal_event = normalize_event(raw_body, source_type=source_type, user_id=user_id)

        user_preferences = raw_body.get("user_preferences")
        result = scheduler.schedule_event(cal_event, user_preferences=user_preferences)

        if result.get("triggered") and "eventbridge_event" in result:
            try:
                emit_eventbridge_prep_event(result["eventbridge_event"])
            except Exception as eb_err:
                # In offline/mock mode or permissions failure, note in response
                result["eventbridge_warning"] = str(eb_err)

        return {
            "statusCode": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(
                {
                    "message": "Calendar event processed",
                    "result": {
                        "status": result.get("status"),
                        "calendar_event_id": cal_event.calendar_event_id,
                        "idempotency_key": result.get("idempotency_key"),
                        "triggered": result.get("triggered", False),
                    },
                }
            ),
        }
    except (ValidationError, ValueError) as err:
        return {
            "statusCode": 400,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": "invalid_calendar_event", "details": str(err)}),
        }
    except Exception as exc:
        return {
            "statusCode": 500,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": "internal_error", "details": str(exc)}),
        }
