"""GatewayServer, assembled from the gateway mixins."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path
from engine.aki_config import state_dir
from engine.letta_process import LettaProcess
from engine.avatar import (
    AvatarError,
    default_avatar_path,
    load_avatar,
)
from engine.channel import TokifactorChannel
from engine.execution_lock import (
    ExecutionLock,
    default_execution_lock_path,
    default_execution_lock_status_path,
)
from engine.live_context import clamp_budget_tokens
from engine.solo_memory import (
    SOLO_MEMORY_BUDGET_TOKENS,
    SoloMemoryManager,
    default_solo_memory_path,
)
from engine.solo_mode import (
    default_solo_state_path,
    derive_solo_magic,
    load_solo_session,
)
from engine.life_ledger import LifeLedger
from engine.operation_journal import OperationJournal
from engine.presence import (
    EvdevActivityProbe,
    LogindActivityProvider,
    PresenceMonitor,
)
from engine.schedule_store import ScheduleStore
from engine.member_profile import MEMBER_KIND_AGENT
from engine.member_identity import (
    IdentityRegistry,
    MemberIdentity,
    is_member_id,
    legacy_member_id,
)
from engine.mqtt_settings import default_mqtt_state_path
from engine.writable import WritableRegistry
from .config import build_letta_command
from .connections import (
    ChannelConnection,
    NullConnection,
)
from .constants import LOGGER
from .paths import (
    default_mcp_native_state_path,
    default_memory_dir,
    default_turn_overlay_dir,
)

from .backend import BackendMixin
from .backend_turn import BackendTurnMixin
from .capture import CaptureMixin
from .channel_history import ChannelHistoryMixin
from .channel_status import ChannelStatusMixin
from .conversation import ConversationMixin
from .delivery import DeliveryMixin
from .exclusive import ExclusiveMixin
from .health import HealthMixin
from .identity import IdentityMixin
from .lifecycle import LifecycleMixin
from .members import MembersMixin
from .memory import MemoryMixin
from .model import ModelMixin
from .mqtt_commands import MqttCommandsMixin
from .mqtt_ingress import MqttIngressMixin
from .overlays import OverlaysMixin
from .proactive import ProactiveMixin
from .prompts import PromptsMixin
from .remote_tools import RemoteToolsMixin
from .solo import SoloMixin
from .turn_finish import TurnFinishMixin
from .turns import TurnsMixin
from .username import UsernameMixin
from .wait_tools import WaitToolsMixin
from .writable_rpc import WritableRpcMixin
class ServerMixin:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        backend_factory: Callable[
            [Callable[[dict[str, Any]], Awaitable[None]], Callable[[dict[str, Any]], Awaitable[None]], Callable[[int | None, str | None], Awaitable[None]]],
            Any,
        ]
        | None = None,
    ) -> None:
        self.config = config
        if not config.channel_name:
            config.channel_name = "Aki" if config.profile == "aki" else config.profile
        if not config.channel_member_id:
            config.channel_member_id = (
                config.channel_id if is_member_id(config.channel_id) else ""
            ) or legacy_member_id(config.channel_name)
        if not config.channel_id:
            config.channel_id = config.channel_member_id
        elif not is_member_id(config.channel_id):
            config.channel_id = config.channel_member_id
        config.mqtt_sender_id = config.channel_member_id
        if config.mqtt_sender_name == "akid":
            config.mqtt_sender_name = config.channel_name
        if not config.channel_aliases:
            config.channel_aliases = (
                ("Aki", "aki", "秋", "小秋", "秋（aki）")
                if config.profile == "aki"
                else (config.channel_name,)
            )
        self._identity_registry = (
            IdentityRegistry(config.identity_registry_path)
            if config.identity_registry_path is not None
            else None
        )
        self._identity_manifest_path = config.identity_manifest_path
        if self._identity_registry is not None:
            existing = self._identity_registry.get(config.channel_member_id)
            created_at = existing.created_at if existing is not None else ""
            identity = MemberIdentity(
                id=config.channel_member_id,
                name=config.channel_name,
                aliases=config.channel_aliases,
                kind=MEMBER_KIND_AGENT,
                created_at=created_at or "",
            )
            claimed, conflicts = self._identity_registry.observe(
                identity, source=f"profile:{config.profile}"
            )
            if not claimed:
                for conflict in conflicts:
                    LOGGER.warning(
                        "local identity label conflict label=%s owner=%s member=%s",
                        conflict.get("label"),
                        conflict.get("owner"),
                        conflict.get("member_id"),
                    )
        self._channel_members: dict[str, dict[str, Any]] = {}
        self._channel_agent_states: dict[str, AgentChannelState] = {}
        self._channel_waiting_user: dict[str, float] = {}
        self._wait_user_active = False
        self._wait_other_active = False
        self._wait_other_targets: tuple[str, ...] = ()
        self._channel_event_seq = 0
        self._channel_history: list[ChannelTranscriptEntry] = []
        self._channel_history_seq = 0
        self._channel_transcript_status: dict[str, Any] = {
            "budget_tokens": self.config.channel_transcript_budget_tokens,
            "entries": 0,
            "last_seq": 0,
            "ephemeral_turns": 0,
        }
        self._channel_transcript_lock = asyncio.Lock()
        self._solo_lock = asyncio.Lock()
        self.channel_history_dir = config.channel_history_dir or (
            (config.profile_config_path.parent / "channel-history")
            if config.profile_config_path is not None
            else (config.backend_dir / "channel-history")
        )
        self.channel_media_dir = config.channel_media_dir or (
            self.channel_history_dir / "media"
        )
        self.solo_state_path = config.solo_state or default_solo_state_path(
            config.profile
        )
        loaded_solo = load_solo_session(self.solo_state_path)
        self._solo_session = (
            loaded_solo
            if loaded_solo is not None
            and loaded_solo.target_id == self._channel_self_key()
            and loaded_solo.profile == config.profile
            else None
        )
        self._solo_targets: dict[str, dict[str, str]] = {}
        self._solo_scene = ""
        self._frozen_group_history: list[ChannelTranscriptEntry] = []
        self._frozen_group_history_seq = 0
        self.group_channel_history_path = self._channel_history_path(solo=False)
        self.solo_channel_history_path = self._channel_history_path(solo=True)
        self.channel_history_path = (
            self.solo_channel_history_path
            if self._solo_session is not None
            else self.group_channel_history_path
        )
        self._channel_pending_images: dict[str, dict[str, Any]] = {}
        self._channel_attachment_tasks: set[asyncio.Task[Any]] = set()
        if self._solo_session is not None:
            self._load_channel_history(self.group_channel_history_path)
            self._frozen_group_history = list(self._channel_history)
            self._frozen_group_history_seq = self._channel_history_seq
            self._load_channel_history(self.solo_channel_history_path)
        else:
            self._load_channel_history()
        self._solo_memory_start_seq = int(self._channel_history_seq)
        self._agent_initiation_remaining = max(
            0, int(config.agent_initiation_limit)
        )
        self.connection_set: set[Connection] = set()
        self.queue: list[Turn] = []
        self.active: Turn | None = None
        self.approvals: dict[str, Approval] = {}
        self.last_usage: dict[str, Any] | None = None
        self.started_at = time.time()
        self._server: asyncio.AbstractServer | None = None
        self._layer: LayerProcess | None = None
        self._layer_start_task: asyncio.Task[None] | None = None
        self._layer_display_name: str | None = None
        self._layer_notifier: LayerNotifier | None = None
        self._layer_skip_reason: str | None = None
        self._client_tasks: set[asyncio.Task[Any]] = set()
        self._approval_tasks: set[asyncio.Task[Any]] = set()
        self._turn_tasks: set[asyncio.Task[Any]] = set()
        self._channel_tasks: set[asyncio.Task[Any]] = set()
        self._external_tool_tasks: set[asyncio.Task[Any]] = set()
        self._backend_queue: deque[dict[str, Any]] = deque()
        self._backend_current_batch: dict[str, Any] | None = None
        self._token_cache: tuple[float, dict[str, Any] | None, str | None] = (0.0, None, None)
        self._token_lock = asyncio.Lock()
        self._status_event = asyncio.Event()
        self._status_stop = asyncio.Event()
        self._status_task: asyncio.Task[None] | None = None
        self._probe_seen: dict[str, float] = {}
        self._probe_rate: dict[str, float] = {}
        self._writable_lock = asyncio.Lock()
        self._mqtt_lock = asyncio.Lock()
        self._reset_lock = asyncio.Lock()
        self._username_lock = asyncio.Lock()
        self._recompile_lock = asyncio.Lock()
        self._compact_lock = asyncio.Lock()
        self._live_context_lock = asyncio.Lock()
        self._tool_ownership_lock = asyncio.Lock()
        self._wake_routing_lock = asyncio.Lock()
        self._kde_detected_at = 0.0
        self._kde_detected = False
        self.config.live_context_budget_tokens = clamp_budget_tokens(
            self.config.live_context_budget_tokens
        )
        self._live_context_status: dict[str, Any] = {
            "enabled": self.config.live_context_enabled,
            "budget_tokens": self.config.live_context_budget_tokens,
            "last_captured_at": None,
            "last_sections": {},
            "last_errors": [],
            "block_bytes": 0,
            "agent_path": None,
            "updated": False,
        }
        self._recompiling = False
        self._compacting = False
        self._resetting = False
        self._policy_restarting = False
        self._shutting_down = False
        self.memory_dir = config.memory_dir or default_memory_dir(config.agent_id)
        solo_memory_path = config.solo_memory_path
        if solo_memory_path is None:
            if config.profile_config_path is not None:
                solo_memory_path = (
                    config.profile_config_path.parent
                    / "solo-memory"
                    / f"{config.profile}.json"
                )
            else:
                solo_memory_path = default_solo_memory_path(config.profile)
        self.solo_memory = SoloMemoryManager(
            profile=config.profile,
            path=solo_memory_path,
            memory_dir=self.memory_dir,
            budget_tokens=SOLO_MEMORY_BUDGET_TOKENS,
            logger=logging.getLogger("akid.solo-memory"),
        )
        self.mqtt_state_path = config.mqtt_state or default_mqtt_state_path()
        self.writable = WritableRegistry(
            state_path=config.writable_state,
            letta_home=config.letta_home,
            backend_dir=config.backend_dir,
            bwrap_bin=config.bwrap_bin,
        )
        self.writable.load()
        self._sandbox_blocked_reason: str | None = None
        self._external_tools_registered = False
        self._external_tools_error: str | None = None
        self._external_tools_pending = False
        self._external_tools_ready = asyncio.Event()
        self._external_tools_ready.set()
        self.operation_journal = OperationJournal()
        state_root = state_dir()
        self.schedule_state = config.schedule_state or (
            state_root / "schedules" / f"{config.profile}.json"
        )
        self.ledger_state = config.ledger_state or (
            state_root / "ledger" / f"{config.profile}.json"
        )
        self.presence_state = config.presence_state or (
            state_root / "presence" / f"{config.profile}.json"
        )
        self.schedule_store = ScheduleStore(
            self.schedule_state,
            max_records=config.schedule_max_records,
            missed_grace_seconds=config.schedule_missed_grace_seconds,
        )
        self.life_ledger = LifeLedger(self.ledger_state)
        self.presence_monitor = PresenceMonitor(
            self.presence_state,
            provider=LogindActivityProvider(
                loginctl_bin=config.presence_loginctl_bin,
                session_id=config.presence_session_id,
                input_probe=EvdevActivityProbe(),
                input_idle_seconds=config.presence_idle_reset_seconds,
            ),
            poll_seconds=config.presence_poll_seconds,
            idle_reset_seconds=config.presence_idle_reset_seconds,
            work_warn_seconds=config.presence_work_warn_seconds,
            work_repeat_seconds=config.presence_work_repeat_seconds,
            night_start=config.presence_night_start,
            night_end=config.presence_night_end,
            night_history_days=config.presence_night_history_days,
            return_idle_seconds=config.presence_return_idle_seconds,
            return_debounce_seconds=config.presence_return_debounce_seconds,
        )
        self._schedule_stop = asyncio.Event()
        self._schedule_task: asyncio.Task[None] | None = None
        self._proactive_pending: dict[str, dict[str, Any]] = {}
        self._proactive_lock = asyncio.Lock()
        self._last_proactive_at: str | None = None
        self._mcp_tools: list[dict[str, Any]] = []
        self._mcp_tool_names: set[str] = set()
        self._mcp_tools_error: str | None = None
        self._mcp_native_active_servers: list[str] = []
        self._mcp_native_excluded: set[str] = set(
            config.mcp_native_excluded_servers
        )
        self.mcp_native_state_path = (
            config.mcp_native_state or default_mcp_native_state_path()
        )
        self._mcp_lock = asyncio.Lock()
        self._load_mcp_native_state()
        lock_path = config.execution_lock_path or default_execution_lock_path()
        self.execution_lock = ExecutionLock(
            path=lock_path,
            status_path=(
                config.execution_lock_status_path
                or default_execution_lock_status_path(lock_path)
            ),
        )
        self.avatar_path = config.avatar_path or default_avatar_path()
        self.avatar: AvatarAsset | None = None
        self.avatar_error: str | None = None
        try:
            self.avatar = load_avatar(self.avatar_path)
        except AvatarError as exc:
            self.avatar_error = str(exc)
            LOGGER.warning("avatar unavailable path=%s error=%s", self.avatar_path, exc)
        self.channel_conn = ChannelConnection(self)
        self.proactive_conn = NullConnection()
        self.channel: TokifactorChannel | None = None
        if config.mqtt_enabled:
            active_magic = (
                derive_solo_magic(config.mqtt_magic, self._channel_self_key())
                if self._solo_session is not None
                else config.mqtt_magic
            )
            self.channel = TokifactorChannel(
                host=config.mqtt_host,
                port=config.mqtt_port,
                magic=active_magic,
                session_magic=config.mqtt_magic,
                topic=config.mqtt_topic,
                sender_name=config.mqtt_sender_name,
                sender_id=config.mqtt_sender_id,
                avatar_bytes=self.avatar.data if self.avatar is not None else None,
                avatar_hash=self.avatar.sha256 if self.avatar is not None else "",
                avatar_mime=self.avatar.mime if self.avatar is not None else "",
                dedup_path=self.channel_history_path.with_suffix(".seen.json"),
                avatar_cache_path=self.channel_history_path.with_suffix(".avatars"),
                on_text=self._on_channel_text,
                on_image=self._on_channel_image,
                on_control=self._on_channel_control,
                on_connected=self._on_channel_connected,
                logger=logging.getLogger("akid.channel"),
            )
        if backend_factory is None:
            backend_factory = self._default_backend_factory
        self.backend = backend_factory(
            self._on_backend_message, self._on_backend_ready, self._on_backend_exit
        )


    def _default_backend_factory(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        on_ready: Callable[[dict[str, Any]], Awaitable[None]],
        on_exit: Callable[[int | None, str | None], Awaitable[None]],
    ) -> Any:
        letta_command = build_letta_command(self.config)
        command = letta_command
        env = dict(os.environ)
        env.update(
            {
                "AKI_PROFILE": self.config.profile,
                "AKI_TURN_OVERLAY_DIR": str(self._turn_overlay_dir()),
                "AKI_EXECUTION_LOCK_STATUS": str(
                    self.execution_lock.status_path
                ),
            }
        )
        if self.config.sandbox_enabled:
            command = [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "sandbox_exec.py"),
                "--state",
                str(self.writable.state_path),
                "--bwrap",
                self.config.bwrap_bin,
                "--",
                *letta_command,
            ]
            env.update(
                {
                    "AKI_SANDBOX_ENABLED": "1",
                    "AKI_BWRAP_BIN": self.config.bwrap_bin,
                    "AKI_WRITABLE_STATE": str(self.writable.state_path),
                    "LETTA_HOME": str(self.writable.letta_home),
                    "LETTA_LOCAL_BACKEND_DIR": str(self.writable.backend_dir),
                }
            )
        return LettaProcess(
            command,
            cwd=str(self.config.cwd),
            env=env,
            on_message=on_message,
            on_ready=on_ready,
            on_exit=on_exit,
            logger=logging.getLogger("akid.letta"),
        )


    def _turn_overlay_dir(self) -> Path:
        configured = os.environ.get("AKI_TURN_OVERLAY_DIR", "").strip()
        if configured:
            return Path(configured).expanduser()
        return default_turn_overlay_dir(
            self.config.backend_dir, self.config.agent_id
        )

class GatewayServer(ServerMixin, LifecycleMixin, HealthMixin, ChannelHistoryMixin, PromptsMixin, TurnsMixin, ProactiveMixin, OverlaysMixin, TurnFinishMixin, WritableRpcMixin, ChannelStatusMixin, MembersMixin, SoloMixin, ModelMixin, MqttIngressMixin, MqttCommandsMixin, BackendMixin, BackendTurnMixin, ExclusiveMixin, WaitToolsMixin, RemoteToolsMixin, CaptureMixin, DeliveryMixin, IdentityMixin, UsernameMixin, ConversationMixin, MemoryMixin):
    pass
