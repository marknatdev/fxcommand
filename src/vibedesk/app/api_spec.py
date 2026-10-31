"""HTTP API specification and schemas (spec §8.1, CCC-50).

Every endpoint in Layer A:
- Address: 127.0.0.1 only, no public login.
- Owner-only actions require the `vibedesk_owner` cookie (SameSite=Strict, set by the UI).
- The MCP server accesses read and act tools without the owner cookie.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Request and Response Models ---


class CreateFamilyIn(BaseModel):
    idea: str = Field(..., min_length=1, description="Owner's trading idea description.")


class FamilySummaryOut(BaseModel):
    id: int
    idea: str
    created_at: str
    hypothesis_count: int = 0
    trial_count: int = 0


class HypothesisDetailOut(BaseModel):
    id: int
    family_id: int
    entry_block: str
    symbol: str
    timeframe: str
    spec: dict[str, Any]
    created_at: str
    trial_ids: list[int] = Field(default_factory=list)


class FamilyDetailOut(BaseModel):
    id: int
    idea: str
    created_at: str
    budget: int
    trial_count: int
    hypotheses: list[HypothesisDetailOut] = Field(default_factory=list)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, description="Owner message in chat.")


class RunTrialIn(BaseModel):
    idea: str = ""


class TrialSummaryOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    trial_id: int
    hypothesis_id: int
    family_id: int
    verdict: Literal["CANDIDATE", "REJECT", "PASS", "HOLDOUT FAIL"]
    label: str
    closest_miss: dict[str, Any] | None = None
    created_at: str
    spec: dict[str, Any]
    summary: dict[str, Any]


class BlockOut(BaseModel):
    name: str
    kind: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class BlockLibraryOut(BaseModel):
    blocks: list[BlockOut]


class FamilyStatsOut(BaseModel):
    family_id: int
    idea: str
    trials: int
    budget: int
    combinations_evaluated: int


class LedgerStatsOut(BaseModel):
    desk_trial_count: int
    current_deflation_combinations: int
    families: list[FamilyStatsOut] = Field(default_factory=list)


class HoldoutRequestIn(BaseModel):
    reason: str = ""


class HoldoutRequestItemOut(BaseModel):
    id: int
    hypothesis_id: int
    family_id: int
    trial_id: int | None = None
    status: str
    created_at: str


class HoldoutRequestOut(BaseModel):
    request_id: int
    hypothesis_id: int
    family_id: int
    status: Literal["pending", "approved", "refused", "spent"]
    refusal_reason: str | None = None
    created_at: str


class HoldoutApproveOut(BaseModel):
    request_id: int
    approved: bool
    grant_token: str
    family_id: int


class BudgetGrantIn(BaseModel):
    amount: int = Field(default=25, ge=1, description="Number of Trials to add to budget.")


class BudgetGrantOut(BaseModel):
    family_id: int
    new_budget: int
    added: int


class PublicSettingsOut(BaseModel):
    llm_url: str
    llm_model: str
    llm_key_set: bool
    daily_token_cap: int
    telegram_enabled: bool
    telegram_chat_id_set: bool
    nightshift_trial_cap: int = 20
    nightshift_token_share: float = 0.50


class UpdateSettingsIn(BaseModel):
    llm_url: str | None = None
    llm_key: str | None = None
    llm_model: str | None = None
    daily_token_cap: int | None = None
    telegram_enabled: bool | None = None
    telegram_chat_id: str | None = None
    nightshift_trial_cap: int | None = None
    nightshift_token_share: float | None = None


# --- Layer B & C Schemas ---


class ArenaStrategyIn(BaseModel):
    hypothesis_id: int


class ArenaStrategyOut(BaseModel):
    id: int
    hypothesis_id: int
    symbol: str
    timeframe: str
    status: str
    twin_type: str
    created_at: str


class ArenaOverrideIn(BaseModel):
    gate_type: Literal["demo_ready", "live_gate"] = "demo_ready"
    reason: str = Field(..., min_length=1)


class LeaderboardOut(BaseModel):
    entries: list[dict[str, Any]] = Field(default_factory=list)


class FillReportOut(BaseModel):
    fills: list[dict[str, Any]] = Field(default_factory=list)


class NightShiftStatusOut(BaseModel):
    status: str
    trials_run: int = 0
    tokens_used: int = 0
    candidates_found: int = 0
    stopped_reason: str = ""
    last_run: str = ""


class RegimeStatusOut(BaseModel):
    evaluated_at: str
    decisions: list[dict[str, Any]] = Field(default_factory=list)


class CustomBlockIn(BaseModel):
    name: str = Field(..., min_length=1)
    kind: str = Field(..., min_length=1)
    code: str = Field(..., min_length=1)


class CustomBlockOut(BaseModel):
    name: str
    kind: str
    approved: bool
    approved_at: str | None = None
    sha256: str


class HealthOut(BaseModel):
    mt5_connected: bool
    omniroute_connected: bool
    last_refresh: str | None
    token_budget_state: str
    tokens_used_today: int
    active_jobs: int


# --- Endpoint Table (spec §8.1) ---

ENDPOINT_TABLE: list[dict[str, Any]] = [
    {
        "area": "Families",
        "method": "GET",
        "path": "/api/families",
        "owner_only": False,
        "request": None,
        "response": "list[FamilySummaryOut]",
        "description": "List all Idea Families with trial counts and budgets.",
    },
    {
        "area": "Families",
        "method": "POST",
        "path": "/api/families",
        "owner_only": False,
        "request": "CreateFamilyIn",
        "response": "FamilyDetailOut",
        "description": "Create a new Idea Family from an Idea text.",
    },
    {
        "area": "Families",
        "method": "GET",
        "path": "/api/families/{id}",
        "owner_only": False,
        "request": None,
        "response": "FamilyDetailOut",
        "description": "Get an Idea Family with its Hypotheses and Trials.",
    },
    {
        "area": "Hypotheses",
        "method": "GET",
        "path": "/api/hypotheses/{id}",
        "owner_only": False,
        "request": None,
        "response": "HypothesisDetailOut",
        "description": "Get a Hypothesis and its Trials.",
    },
    {
        "area": "Chat",
        "method": "POST",
        "path": "/api/families/{id}/chat",
        "owner_only": False,
        "request": "ChatIn",
        "response": "text/event-stream",
        "description": "Desk loop SSE event stream (message, tool, progress, verdict).",
    },
    {
        "area": "Trials",
        "method": "POST",
        "path": "/api/hypotheses/{id}/trials",
        "owner_only": False,
        "request": "RunTrialIn",
        "response": "TrialSummaryOut",
        "description": "Run the Gauntlet on a Hypothesis. Counts as 1 Trial.",
    },
    {
        "area": "Trials",
        "method": "GET",
        "path": "/api/trials/{id}",
        "owner_only": False,
        "request": None,
        "response": "TrialSummaryOut",
        "description": "Get a Trial summary with its Verdict Card data.",
    },
    {
        "area": "Blocks",
        "method": "GET",
        "path": "/api/blocks",
        "owner_only": False,
        "request": None,
        "response": "BlockLibraryOut",
        "description": "Get the Block Library.",
    },
    {
        "area": "Ledger",
        "method": "GET",
        "path": "/api/ledger",
        "owner_only": False,
        "request": None,
        "response": "LedgerStatsOut",
        "description": "Desk-wide and per-family Trial counts and deflation inputs.",
    },
    {
        "area": "Holdout",
        "method": "GET",
        "path": "/api/holdout-requests",
        "owner_only": False,
        "request": None,
        "response": "list[HoldoutRequestItemOut]",
        "description": "List Holdout Requests, optionally filtered by status.",
    },
    {
        "area": "Holdout",
        "method": "POST",
        "path": "/api/hypotheses/{id}/holdout-requests",
        "owner_only": False,
        "request": "HoldoutRequestIn",
        "response": "HoldoutRequestOut",
        "description": "Submit a Holdout Request through the gate in spec §6.3.",
    },
    {
        "area": "Holdout",
        "method": "POST",
        "path": "/api/holdout-requests/{id}/approve",
        "owner_only": True,
        "request": None,
        "response": "HoldoutApproveOut",
        "description": "Approve a Holdout Request and issue a HoldoutGrant (owner-only).",
    },
    {
        "area": "Budget",
        "method": "POST",
        "path": "/api/families/{id}/budget",
        "owner_only": True,
        "request": "BudgetGrantIn",
        "response": "BudgetGrantOut",
        "description": "Extend an Idea Family's Trial Budget by +25 (owner-only).",
    },
    {
        "area": "Settings",
        "method": "GET",
        "path": "/api/settings",
        "owner_only": False,
        "request": None,
        "response": "PublicSettingsOut",
        "description": "Get public settings (keys masked).",
    },
    {
        "area": "Settings",
        "method": "PUT",
        "path": "/api/settings",
        "owner_only": True,
        "request": "UpdateSettingsIn",
        "response": "PublicSettingsOut",
        "description": "Update settings and secrets (owner-only).",
    },
    {
        "area": "Health",
        "method": "GET",
        "path": "/api/health",
        "owner_only": False,
        "request": None,
        "response": "HealthOut",
        "description": "Check terminal, OmniRoute, refresh timestamp, and budget state.",
    },
]

# --- Layer B & C Endpoint Table (docs/spec/layer-b-c.md) ---

LAYER_BC_ENDPOINT_TABLE: list[dict[str, Any]] = [
    # Layer B: Arena & Leaderboard
    {
        "area": "Arena",
        "method": "GET",
        "path": "/api/arena/strategies",
        "owner_only": False,
        "request": None,
        "response": "list[ArenaStrategyOut]",
        "description": "List all enrolled Forward Arena strategies.",
    },
    {
        "area": "Arena",
        "method": "POST",
        "path": "/api/arena/strategies",
        "owner_only": True,
        "request": "ArenaStrategyIn",
        "response": "ArenaStrategyOut",
        "description": "Enroll a CANDIDATE strategy into the Forward Arena (owner-only).",
    },
    {
        "area": "Arena",
        "method": "POST",
        "path": "/api/arena/strategies/{id}/pause",
        "owner_only": True,
        "request": None,
        "response": "dict[str, Any]",
        "description": "Pause an Arena strategy (owner-only).",
    },
    {
        "area": "Arena",
        "method": "POST",
        "path": "/api/arena/strategies/{id}/resume",
        "owner_only": True,
        "request": None,
        "response": "dict[str, Any]",
        "description": "Resume an Arena strategy (owner-only).",
    },
    {
        "area": "Arena",
        "method": "DELETE",
        "path": "/api/arena/strategies/{id}",
        "owner_only": True,
        "request": None,
        "response": "dict[str, Any]",
        "description": "Remove an Arena strategy (owner-only).",
    },
    {
        "area": "Arena",
        "method": "POST",
        "path": "/api/arena/stop",
        "owner_only": True,
        "request": None,
        "response": "dict[str, Any]",
        "description": "Emergency global stop for Forward Arena (owner-only).",
    },
    {
        "area": "Arena",
        "method": "GET",
        "path": "/api/arena/leaderboard",
        "owner_only": False,
        "request": None,
        "response": "LeaderboardOut",
        "description": "Get Forward Arena leaderboard ranked by net R.",
    },
    {
        "area": "Arena",
        "method": "POST",
        "path": "/api/arena/strategies/{id}/override",
        "owner_only": True,
        "request": "ArenaOverrideIn",
        "response": "dict[str, Any]",
        "description": "Mark strategy Demo-Ready anyway via audited owner override (owner-only).",
    },
    {
        "area": "Arena",
        "method": "GET",
        "path": "/api/arena/fills/report",
        "owner_only": False,
        "request": None,
        "response": "FillReportOut",
        "description": "Compare backtest-cost track against live-spread track.",
    },
    {
        "area": "Arena",
        "method": "GET",
        "path": "/api/arena/uptime",
        "owner_only": False,
        "request": None,
        "response": "dict[str, Any]",
        "description": "Get host uptime, bar gap history and health status.",
    },
    # Layer C: Night Shift, Regime Desk & Custom Blocks
    {
        "area": "NightShift",
        "method": "GET",
        "path": "/api/nightshift/status",
        "owner_only": False,
        "request": None,
        "response": "NightShiftStatusOut",
        "description": "Get status and results of recent Night Shift runs.",
    },
    {
        "area": "NightShift",
        "method": "POST",
        "path": "/api/nightshift/run",
        "owner_only": True,
        "request": None,
        "response": "NightShiftStatusOut",
        "description": "Manually trigger Night Shift exploration loop (owner-only).",
    },
    {
        "area": "Regime",
        "method": "GET",
        "path": "/api/regime/status",
        "owner_only": False,
        "request": None,
        "response": "RegimeStatusOut",
        "description": "Get daily market features and regime ON/OFF decisions for strategy twins.",
    },
    {
        "area": "CustomBlocks",
        "method": "GET",
        "path": "/api/blocks/custom",
        "owner_only": False,
        "request": None,
        "response": "list[CustomBlockOut]",
        "description": "List AI-authored custom blocks and approval states.",
    },
    {
        "area": "CustomBlocks",
        "method": "POST",
        "path": "/api/blocks/custom",
        "owner_only": False,
        "request": "CustomBlockIn",
        "response": "CustomBlockOut",
        "description": "Submit a proposed custom block for validation.",
    },
    {
        "area": "CustomBlocks",
        "method": "POST",
        "path": "/api/blocks/custom/{name}/approve",
        "owner_only": True,
        "request": None,
        "response": "CustomBlockOut",
        "description": "Approve a custom block for use in Trials (owner-only).",
    },
]


ALL_ENDPOINT_TABLE: list[dict[str, Any]] = ENDPOINT_TABLE + LAYER_BC_ENDPOINT_TABLE

# --- MCP Tool Map (spec §8.2) ---

MCP_TOOL_MAP: dict[str, dict[str, str]] = {
    # Read tools
    "list_ideas": {"method": "GET", "path": "/api/families", "kind": "read"},
    "get_verdict": {"method": "GET", "path": "/api/trials/{id}", "kind": "read"},
    "get_ledger_stats": {"method": "GET", "path": "/api/ledger", "kind": "read"},
    "list_blocks": {"method": "GET", "path": "/api/blocks", "kind": "read"},
    "get_trial": {"method": "GET", "path": "/api/trials/{id}", "kind": "read"},
    # Act tools (counted in the Ledger)
    "submit_idea": {"method": "POST", "path": "/api/families", "kind": "act"},
    "run_trial": {"method": "POST", "path": "/api/hypotheses/{id}/trials", "kind": "act"},
    "request_holdout": {
        "method": "POST",
        "path": "/api/hypotheses/{id}/holdout-requests",
        "kind": "act",
    },
}


__all__ = [
    "ALL_ENDPOINT_TABLE",
    "ENDPOINT_TABLE",
    "LAYER_BC_ENDPOINT_TABLE",
    "MCP_TOOL_MAP",
    "BlockLibraryOut",
    "BlockOut",
    "BudgetGrantIn",
    "BudgetGrantOut",
    "ChatIn",
    "CreateFamilyIn",
    "FamilyDetailOut",
    "FamilyStatsOut",
    "FamilySummaryOut",
    "HealthOut",
    "HoldoutApproveOut",
    "HoldoutRequestIn",
    "HoldoutRequestItemOut",
    "HoldoutRequestOut",
    "HypothesisDetailOut",
    "PublicSettingsOut",
    "RunTrialIn",
    "ArenaOverrideIn",
    "ArenaStrategyIn",
    "ArenaStrategyOut",
    "CustomBlockIn",
    "CustomBlockOut",
    "FillReportOut",
    "LeaderboardOut",
    "NightShiftStatusOut",
    "RegimeStatusOut",
    "TrialSummaryOut",
    "UpdateSettingsIn",
]
