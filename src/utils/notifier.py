"""Notification dispatcher: email-to-Slack channel or Slack webhook.

Configure via config.yaml:
  notifications.method: email | slack_webhook
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx

from src.config import NotificationConfig

logger = logging.getLogger(__name__)


class RunSummary:
    def __init__(
        self,
        mode: str,
        fetched: int = 0,
        evaluated: int = 0,
        published: int = 0,
        errors: int = 0,
        error_details: Optional[list[str]] = None,
    ) -> None:
        self.mode = mode
        self.fetched = fetched
        self.evaluated = evaluated
        self.published = published
        self.errors = errors
        self.error_details = error_details or []
        self.started_at = datetime.now(timezone.utc)

    def is_failure(self) -> bool:
        return self.errors > 0 and self.evaluated == 0

    def is_partial_failure(self) -> bool:
        return self.errors > 0 and self.evaluated > 0

    def elapsed_str(self) -> str:
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        m, s = divmod(int(elapsed), 60)
        return f"{m}m {s}s"

    def subject(self) -> str:
        if self.is_failure():
            return "🔴 Ticket Evaluator — Pipeline FAILED"
        if self.is_partial_failure():
            return f"🟡 Ticket Evaluator — Completed with {self.errors} error(s)"
        return f"✅ Ticket Evaluator — {self.evaluated} tickets evaluated"

    def body(self) -> str:
        lines = [
            f"Run mode: {self.mode}",
            f"Duration: {self.elapsed_str()}",
            "",
            f"  Tickets fetched:   {self.fetched}",
            f"  Tickets evaluated: {self.evaluated}",
            f"  Results published: {self.published}",
            f"  Errors:            {self.errors}",
        ]
        if self.error_details:
            lines += ["", "Error details:"]
            lines += [f"  - {e}" for e in self.error_details[:10]]
            if len(self.error_details) > 10:
                lines.append(f"  ... and {len(self.error_details) - 10} more")
        return "\n".join(lines)


class Notifier:
    def __init__(self, config: NotificationConfig) -> None:
        self._config = config

    async def send_summary(self, summary: RunSummary) -> None:
        cfg = self._config
        should_send = (
            (cfg.on_completion and not summary.is_failure() and not summary.is_partial_failure())
            or (cfg.on_failure and summary.is_failure())
            or (cfg.on_partial_failure and summary.is_partial_failure())
        )
        if not should_send:
            return

        if cfg.method == "slack_webhook":
            await self._send_webhook(summary)
        else:
            await self._send_email(summary)

    async def send_fatal(self, message: str) -> None:
        """Send an immediate alert for fatal pipeline errors."""
        if not self._config.on_failure:
            return
        summary = RunSummary(mode="error", errors=1, error_details=[message])
        # Force send regardless of other flags
        if self._config.method == "slack_webhook":
            await self._send_webhook(summary)
        else:
            await self._send_email(summary)

    # ------------------------------------------------------------------
    # Email-to-Slack (or any SMTP recipient)
    # ------------------------------------------------------------------

    async def _send_email(self, summary: RunSummary) -> None:
        ec = self._config.email
        if not ec.to_address or not ec.username:
            logger.warning("Email notifications not configured — skipping")
            return

        msg = MIMEMultipart("alternative")
        msg["Subject"] = summary.subject()
        msg["From"] = ec.from_address or ec.username
        msg["To"] = ec.to_address

        text_body = summary.body()
        msg.attach(MIMEText(text_body, "plain"))

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(ec.smtp_host, ec.smtp_port) as server:
                if ec.use_tls:
                    server.starttls(context=context)
                server.login(ec.username, ec.password)
                server.sendmail(ec.from_address or ec.username, ec.to_address, msg.as_string())
            logger.info("Notification email sent to %s", ec.to_address)
        except Exception as exc:
            logger.error("Failed to send notification email: %s", exc)

    # ------------------------------------------------------------------
    # Slack webhook (fallback)
    # ------------------------------------------------------------------

    async def _send_webhook(self, summary: RunSummary) -> None:
        url = self._config.slack_webhook.webhook_url
        if not url:
            logger.warning("Slack webhook URL not configured — skipping")
            return

        payload = {
            "text": summary.subject(),
            "attachments": [
                {
                    "color": "#ff0000" if summary.is_failure() else
                             "#ffaa00" if summary.is_partial_failure() else "#36a64f",
                    "text": f"```{summary.body()}```",
                }
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            logger.info("Slack webhook notification sent")
        except Exception as exc:
            logger.error("Failed to send Slack webhook notification: %s", exc)
