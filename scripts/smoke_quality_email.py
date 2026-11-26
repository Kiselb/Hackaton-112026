from __future__ import annotations

import argparse
from datetime import UTC, datetime
from uuid import uuid4

from clip_agent.config import get_settings
from clip_agent.db import QualityRequest
from clip_agent.quality import _send_message, build_notification_message


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicit live SMTP smoke: sends exactly one synthetic notification."
    )
    parser.add_argument(
        "--confirm-send-one-email",
        action="store_true",
        help="Required acknowledgement that one real email will be sent.",
    )
    args = parser.parse_args()
    settings = get_settings()
    if not settings.quality_email_enabled:
        print("SKIPPED: QUALITY_EMAIL_ENABLED is false")
        return 0
    required = (
        settings.quality_email_to,
        settings.quality_smtp_host,
        settings.quality_smtp_from,
    )
    if not all(required):
        print("SKIPPED: SMTP configuration is incomplete")
        return 0
    if not args.confirm_send_one_email:
        print("SKIPPED: pass --confirm-send-one-email to send one synthetic message")
        return 0
    now = datetime.now(UTC)
    request = QualityRequest(
        id=uuid4(),
        public_number="QA-SMOKE",
        trace_id=uuid4(),
        conversation_id=uuid4(),
        idempotency_key=f"live-smoke-{uuid4()}",
        question="Синтетическая проверка канала уведомлений CLIP-015.",
        reason_code="live_smtp_smoke",
        source_outcome="quality_review_required",
        clarification_context={"answers": ["Корпоративные данные не включены."]},
        status="awaiting_expert",
        email_status="sending",
        created_at=now,
        updated_at=now,
    )
    _send_message(build_notification_message(request, settings), settings)
    print("SENT: SMTP server accepted one synthetic CLIP-015 smoke notification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
