"""Configuration loader.

Reads config/config.yaml, expands ${ENV_VAR} references from environment
(loaded from .env), and exposes a typed AppConfig object.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Env expansion
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _expand(value: Any) -> Any:
    """Recursively expand ${VAR} references in strings."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            var = m.group(1)
            return os.environ.get(var, "")
        return _ENV_VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(i) for i in value]
    return value


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ZendeskGroup:
    id: str
    name: str


@dataclass
class ZendeskRateLimit:
    regular_requests_per_minute: int = 400
    export_requests_per_minute: int = 8


@dataclass
class ZendeskConfig:
    subdomain: str
    email: str
    api_token: str
    groups: list[ZendeskGroup]
    ticket_status: str = "closed"
    rate_limit: ZendeskRateLimit = field(default_factory=ZendeskRateLimit)

    @property
    def group_ids(self) -> list[str]:
        return [g.id for g in self.groups]


@dataclass
class LLMProviderConfig:
    model: str
    api_key: str
    max_tokens: int = 8000
    temperature: float = 0
    max_input_tokens: int = 90000


@dataclass
class LLMRateLimit:
    requests_per_minute: int = 50
    tokens_per_minute: int = 100000


@dataclass
class LLMConfig:
    provider: str  # claude | openai
    claude: LLMProviderConfig
    openai: LLMProviderConfig
    rate_limit: LLMRateLimit = field(default_factory=LLMRateLimit)

    @property
    def active(self) -> LLMProviderConfig:
        return self.claude if self.provider == "claude" else self.openai


@dataclass
class PipelineConfig:
    concurrent_fetches: int = 3
    concurrent_evaluations: int = 2
    queue_size: int = 50


@dataclass
class SLATier:
    """Thresholds for one severity/priority tier within a channel."""
    frt_seconds: Optional[int] = None    # chat FRT threshold
    frt_minutes: Optional[int] = None    # email FRT threshold
    ttr_minutes: int = 120
    weekend_exclusion: bool = True
    timezone: str = "Asia/Kolkata"


@dataclass
class SLAChannelConfig:
    """Per-channel SLA config with a default tier and optional severity overrides."""
    default: SLATier
    tiers: dict = field(default_factory=dict)  # severity_key → SLATier

    def resolve(self, severity_key: Optional[str]) -> SLATier:
        """Return the tier matching severity_key, or fall back to default."""
        if severity_key and severity_key in self.tiers:
            return self.tiers[severity_key]
        return self.default


@dataclass
class SLAConfig:
    chat: SLAChannelConfig = field(default_factory=lambda: SLAChannelConfig(default=SLATier(frt_seconds=30, ttr_minutes=120)))
    email: SLAChannelConfig = field(default_factory=lambda: SLAChannelConfig(default=SLATier(frt_minutes=30, ttr_minutes=2880)))
    severity_field_id: Optional[str] = None


@dataclass
class EvaluationConfig:
    prompt_file: str
    prompt_version: str
    skip_if_evaluated: bool = True
    sla: SLAConfig = field(default_factory=SLAConfig)
    breach_minor_multiplier: float = 1.2


@dataclass
class WriteBackFields:
    aggregate_score: Optional[str] = None
    evaluation_date: Optional[str] = None
    evaluator_confidence: Optional[str] = None
    prompt_version: Optional[str] = None
    frt_status: Optional[str] = None
    ttr_status: Optional[str] = None
    llm_provider: Optional[str] = None


@dataclass
class ZendeskWriteBackConfig:
    enabled: bool = True
    custom_fields: WriteBackFields = field(default_factory=WriteBackFields)
    metric_fields: dict = field(default_factory=dict)  # METRIC_N -> field_id


@dataclass
class StateConfig:
    file: str = "data/state.json"
    initial_fetch_from: str = "2025-01-01T00:00:00Z"


@dataclass
class OutputConfig:
    tickets_dir: str = "data/tickets"
    evaluations_dir: str = "data/evaluations"
    exports_dir: str = "data/exports"
    database: str = "data/evaluations.db"
    export_csv: bool = True


@dataclass
class EmailNotificationConfig:
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    use_tls: bool = True
    username: str = ""
    password: str = ""
    from_address: str = ""
    to_address: str = ""


@dataclass
class SlackWebhookConfig:
    webhook_url: str = ""


@dataclass
class NotificationConfig:
    method: str = "email"  # email | slack_webhook
    email: EmailNotificationConfig = field(default_factory=EmailNotificationConfig)
    on_completion: bool = True
    on_failure: bool = True
    on_partial_failure: bool = True
    slack_webhook: SlackWebhookConfig = field(default_factory=SlackWebhookConfig)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/evaluator.log"
    max_bytes: int = 10485760
    backup_count: int = 5
    console: bool = True


@dataclass
class AppConfig:
    zendesk: ZendeskConfig
    llm: LLMConfig
    pipeline: PipelineConfig
    evaluation: EvaluationConfig
    zendesk_write_back: ZendeskWriteBackConfig
    state: StateConfig
    output: OutputConfig
    notifications: NotificationConfig
    logging: LoggingConfig


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def load_config(config_path: str | Path = "config/config.yaml") -> AppConfig:
    """Load and parse the application config, expanding env variables."""
    load_dotenv(override=False)

    raw = yaml.safe_load(Path(config_path).read_text())
    raw = _expand(raw)

    z = raw["zendesk"]
    rl = z.get("rate_limit", {})
    groups = [ZendeskGroup(id=str(g["id"]), name=g.get("name", "")) for g in z.get("groups", [])]
    zendesk = ZendeskConfig(
        subdomain=z["subdomain"],
        email=z["email"],
        api_token=z["api_token"],
        groups=groups,
        ticket_status=z.get("ticket_status", "closed"),
        rate_limit=ZendeskRateLimit(
            regular_requests_per_minute=_int(rl.get("regular_requests_per_minute"), 400),
            export_requests_per_minute=_int(rl.get("export_requests_per_minute"), 8),
        ),
    )

    lm = raw["llm"]
    lrl = lm.get("rate_limit", {})
    cl = lm.get("claude", {})
    oa = lm.get("openai", {})
    llm = LLMConfig(
        provider=lm.get("provider", "claude"),
        claude=LLMProviderConfig(
            model=cl.get("model", "claude-opus-4-6"),
            api_key=cl.get("api_key", ""),
            max_tokens=_int(cl.get("max_tokens"), 8000),
            temperature=_float(cl.get("temperature"), 0),
            max_input_tokens=_int(cl.get("max_input_tokens"), 90000),
        ),
        openai=LLMProviderConfig(
            model=oa.get("model", "gpt-4o"),
            api_key=oa.get("api_key", ""),
            max_tokens=_int(oa.get("max_tokens"), 8000),
            temperature=_float(oa.get("temperature"), 0),
            max_input_tokens=_int(oa.get("max_input_tokens"), 90000),
        ),
        rate_limit=LLMRateLimit(
            requests_per_minute=_int(lrl.get("requests_per_minute"), 50),
            tokens_per_minute=_int(lrl.get("tokens_per_minute"), 100000),
        ),
    )

    pl = raw.get("pipeline", {})
    pipeline = PipelineConfig(
        concurrent_fetches=_int(pl.get("concurrent_fetches"), 3),
        concurrent_evaluations=_int(pl.get("concurrent_evaluations"), 2),
        queue_size=_int(pl.get("queue_size"), 50),
    )

    ev = raw["evaluation"]
    sl = ev.get("sla", {})

    def _load_sla_tier(raw_tier: dict, is_email: bool) -> SLATier:
        if is_email:
            raw_frt_min = raw_tier.get("frt_minutes")
            return SLATier(
                frt_minutes=_int(raw_frt_min, 30) if raw_frt_min is not None else 30,
                ttr_minutes=_int(raw_tier.get("ttr_minutes"), 2880),
                weekend_exclusion=_bool(raw_tier.get("weekend_exclusion"), True),
                timezone=raw_tier.get("timezone", "Asia/Kolkata"),
            )
        raw_frt_sec = raw_tier.get("frt_seconds")
        return SLATier(
            frt_seconds=_int(raw_frt_sec, 30) if raw_frt_sec is not None else 30,
            ttr_minutes=_int(raw_tier.get("ttr_minutes"), 120),
        )

    def _load_sla_channel(ch: dict, is_email: bool) -> SLAChannelConfig:
        if "default" in ch:
            # New tiered format
            default = _load_sla_tier(ch["default"], is_email)
            tiers = {
                k: _load_sla_tier(v, is_email)
                for k, v in ch.items()
                if k != "default" and isinstance(v, dict)
            }
        else:
            # Old flat format — auto-wrap in default tier (backward compat)
            default = _load_sla_tier(ch, is_email)
            tiers = {}
        return SLAChannelConfig(default=default, tiers=tiers)

    evaluation = EvaluationConfig(
        prompt_file=ev["prompt_file"],
        prompt_version=str(ev.get("prompt_version", "v1")),
        skip_if_evaluated=_bool(ev.get("skip_if_evaluated"), True),
        sla=SLAConfig(
            chat=_load_sla_channel(sl.get("chat", {}), is_email=False),
            email=_load_sla_channel(sl.get("email", {}), is_email=True),
            severity_field_id=sl.get("severity_field_id") or None,
        ),
        breach_minor_multiplier=_float(ev.get("breach_minor_multiplier"), 1.2),
    )

    wb = raw.get("zendesk_write_back", {})
    cf = wb.get("custom_fields", {}) or {}
    write_back = ZendeskWriteBackConfig(
        enabled=_bool(wb.get("enabled"), True),
        custom_fields=WriteBackFields(
            aggregate_score=cf.get("aggregate_score") or None,
            evaluation_date=cf.get("evaluation_date") or None,
            evaluator_confidence=cf.get("evaluator_confidence") or None,
            prompt_version=cf.get("prompt_version") or None,
            frt_status=cf.get("frt_status") or None,
            ttr_status=cf.get("ttr_status") or None,
            llm_provider=cf.get("llm_provider") or None,
        ),
        metric_fields={k: v for k, v in (wb.get("metric_fields") or {}).items() if v},
    )

    st = raw.get("state", {})
    state = StateConfig(
        file=st.get("file", "data/state.json"),
        initial_fetch_from=st.get("initial_fetch_from", "2025-01-01T00:00:00Z"),
    )

    op = raw.get("output", {})
    output = OutputConfig(
        tickets_dir=op.get("tickets_dir", "data/tickets"),
        evaluations_dir=op.get("evaluations_dir", "data/evaluations"),
        exports_dir=op.get("exports_dir", "data/exports"),
        database=op.get("database", "data/evaluations.db"),
        export_csv=_bool(op.get("export_csv"), True),
    )

    nt = raw.get("notifications", {})
    em = nt.get("email", {}) or {}
    sw = nt.get("slack_webhook", {}) or {}
    notifications = NotificationConfig(
        method=nt.get("method", "email"),
        email=EmailNotificationConfig(
            smtp_host=em.get("smtp_host", "smtp.gmail.com"),
            smtp_port=_int(em.get("smtp_port"), 587),
            use_tls=_bool(em.get("use_tls"), True),
            username=em.get("username", ""),
            password=em.get("password", ""),
            from_address=em.get("from_address", ""),
            to_address=em.get("to_address", ""),
        ),
        on_completion=_bool(nt.get("on_completion"), True),
        on_failure=_bool(nt.get("on_failure"), True),
        on_partial_failure=_bool(nt.get("on_partial_failure"), True),
        slack_webhook=SlackWebhookConfig(webhook_url=sw.get("webhook_url", "")),
    )

    lg = raw.get("logging", {})
    logging_cfg = LoggingConfig(
        level=lg.get("level", "INFO"),
        file=lg.get("file", "logs/evaluator.log"),
        max_bytes=_int(lg.get("max_bytes"), 10485760),
        backup_count=_int(lg.get("backup_count"), 5),
        console=_bool(lg.get("console"), True),
    )

    return AppConfig(
        zendesk=zendesk,
        llm=llm,
        pipeline=pipeline,
        evaluation=evaluation,
        zendesk_write_back=write_back,
        state=state,
        output=output,
        notifications=notifications,
        logging=logging_cfg,
    )
