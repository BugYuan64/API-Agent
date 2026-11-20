"""Bound and redact task evidence before persistence or live publication."""

import json

from api_agent.infrastructure.privacy import Context


class TaskEvidence:
    """Apply task-specific privacy and size limits to transport evidence."""

    @staticmethod
    def redact_body(value):
        """Redact labelled credentials in text or JSON event payloads."""
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return Context().text(value)
        if not isinstance(parsed, (dict, list)):
            return Context().text(value)
        context = Context()
        context.learn(parsed)
        return json.dumps(context.clean(parsed), ensure_ascii=False)

    @staticmethod
    def compact(evidence):
        """Bound persisted body and event previews without changing totals."""
        response = evidence.get("response")
        if response and isinstance(response.get("body"), str) and len(response["body"]) > 16000:
            response["body"] = response["body"][:16000]
            response["body_truncated"] = True
        events = ((response or {}).get("sse") or {}).get("events")
        if events is None:
            events = evidence.get("events")
        if not isinstance(events, list):
            return evidence
        for event in events:
            data = event.get("data")
            if isinstance(data, str) and len(data) > 1000:
                event["data"] = data[:1000]
                event["truncated"] = True
        if len(events) > 40:
            if response and response.get("sse"):
                response["sse"].update(
                    total_events=len(events), events_truncated=True, events=events[:40]
                )
            else:
                evidence.update(total_events=len(events), events_truncated=True, events=events[:40])
        return evidence
