import {
  Alert,
  Button,
  FileDownload,
  ButtonGroup,
  Card,
  DataTable,
  Divider,
  EmptyState,
  Field,
  Grid,
  Heading,
  Inline,
  InlineError,
  Input,
  KeyValue,
  Page,
  Slider,
  Stack,
  StatCard,
  StatusBadge,
  Switch,
  Text,
  Warning,
  useConfirm,
  useEffect,
  useMemo,
  useRef,
  useState,
  useToast,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps, Tone } from "@neko/plugin-ui"

type BoardState = {
  count?: number
  attack?: number
  health?: number
  cards?: string[]
}

type SideState = {
  health?: number | null
  armor?: number
  effective_health?: number | null
  mana_available?: number | null
  mana_max?: number | null
  hand_count?: number
  deck_count?: number
  secret_count?: number
  board?: BoardState
}

type RecentCard = {
  side?: string
  card?: string
  card_id?: string
  turn?: number
}

type RecentCardRow = RecentCard & {
  _key: string
}

type RuntimeState = {
  monitor_running?: boolean
  source_state?: string
  resolved_log_path?: string
  lines_seen?: number
  events_seen?: number
  llm_submissions?: number
  last_line_at?: number
  last_state_at?: number
  last_event_at?: number
  last_event_kind?: string
  last_error_code?: string
  snapshot_revision?: number
}

type RouteDiagnosticState = {
  status?: string
  reason?: string
  observed_at?: number
  mode?: string
  focus?: string
}

type DiagnosticHealthState = {
  log?: {
    fresh?: boolean
    line_age_seconds?: number | null
    state_age_seconds?: number | null
    freshness_limit_seconds?: number
  }
  snapshot?: {
    source_generation?: number
    revision?: number
    mode?: string
    phase?: string
    game_number?: number
    round?: number
  }
  tool_registration?: {
    status?: string
    reason?: string
    checked_at?: number
    missing?: string[]
    recovered_count?: number
    error_code?: string
    check_in_flight?: boolean
    retry_after_seconds?: number
  }
  routes?: {
    agent?: RouteDiagnosticState
    lifecycle?: RouteDiagnosticState
    llm_tool?: RouteDiagnosticState
  }
}

type GameState = {
  mode?: string
  phase?: string
  game_number?: number
  turn?: number
  round?: number
  active_side?: string
  player?: SideState
  opponent?: SideState
  recent_cards?: RecentCard[]
  result?: string
  battlegrounds?: BattlegroundsState | null
}

type BattlegroundsCard = {
  card_id?: string
  name?: string
  card_type?: string | null
  attack?: number
  health?: number | null
  tier?: number
  frozen?: boolean
  position?: number
  premium?: boolean | null
  current_cost?: number | null
  keywords?: Record<string, boolean | null>
}

type BattlegroundsChoice = {
  choice_type?: string
  count_min?: number
  count_max?: number
  source?: BattlegroundsCard | null
  options?: BattlegroundsCard[]
}

type BattlegroundsEconomy = {
  upgrade_cost?: number | null
  refresh_cost?: number | null
  revision?: number
  observed_at?: number | null
}

type BattlegroundsArea = {
  complete?: boolean
  revision?: number
  observed_at?: number | null
  round?: number
  phase?: string
}

type BattlegroundsHeroChoice = {
  card_id?: string
  name?: string
}

type BattlegroundsLobbyPlayer = {
  player_id?: number
  is_local?: boolean
  hero_card_id?: string
  hero_name?: string
  health?: number | null
  armor?: number
  effective_health?: number | null
  tavern_tier?: number
  triples?: number
  placement?: number
  eliminated?: boolean
  next_opponent?: boolean
  current_opponent?: boolean
  last_opponent?: boolean
  is_teammate?: boolean
  last_seen_round?: number
  board?: BoardState & { observed_in_combat?: boolean; observed_round?: number }
}

type BattlegroundsState = {
  variant?: string
  round?: number
  phase?: string
  gold?: number | null
  max_gold?: number | null
  tavern_tier?: number
  frozen?: boolean
  next_opponent_player_id?: number
  current_opponent_player_id?: number
  last_opponent_player_id?: number
  last_opponent_round?: number
  placement?: number
  refresh_cost?: number | null
  upgrade_cost?: number | null
  hero_choices?: BattlegroundsHeroChoice[]
  shop?: BattlegroundsCard[]
  hand?: BattlegroundsCard[]
  warband?: BattlegroundsCard[]
  lobby?: BattlegroundsLobbyPlayer[]
  current_choice?: BattlegroundsChoice | null
  economy?: BattlegroundsEconomy
  areas?: Record<string, BattlegroundsArea>
  mechanics?: Record<string, unknown>
  source?: string
}

type BattlegroundsModeStats = {
  games?: number
  top4?: number
  top4_rate?: number | null
  top2?: number
  top2_rate?: number | null
  first?: number
  first_rate?: number | null
  average_placement?: number | null
  heroes?: Record<string, BattlegroundsModeStats>
}

type BattlegroundsStatsState = {
  schema_version?: number
  seasons?: Record<string, { solo?: BattlegroundsModeStats; duos?: BattlegroundsModeStats }>
}

type BattlegroundsSeasonState = {
  key?: string
  season?: number | null
  patch?: string
  name?: string
  verified_at?: string
  source_url?: string
  status?: string
  mechanics?: { id?: string; title?: string; summary?: string }[]
}

type OverlayState = {
  available?: boolean
  reason?: string
  running?: boolean
  pid?: number
}

type SettingsState = {
  log_path?: string
  llm_do_not_disturb?: boolean
  llm_data_consent?: boolean
  target_lanlan?: string
  card_catalog_network_enabled?: boolean
  overlay_enabled?: boolean
  overlay_height_percent?: number
  overlay_font_size?: number
  overlay_speed_px_per_second?: number
}

type PrivacyState = {
  raw_log_uploaded?: boolean
  player_names_retained?: boolean
  hidden_opponent_cards_exposed?: boolean
  llm_public_state_sharing_enabled?: boolean
  llm_lifecycle_reactions_enabled?: boolean
  card_catalog_network_enabled?: boolean
  card_catalog_sends_game_state?: boolean
}

type DashboardState = {
  runtime?: RuntimeState
  game?: GameState
  overlay?: OverlayState
  settings?: SettingsState
  privacy?: PrivacyState
  battlegrounds_stats?: BattlegroundsStatsState
  battlegrounds_stats_storage?: { degraded?: boolean; error_code?: string }
  battlegrounds_season?: BattlegroundsSeasonState
  card_catalog?: {
    available?: boolean
    card_count?: number
    degraded_reason?: string
    dataset?: { provider?: string; patch?: string; checked_at?: number; stale?: boolean }
  }
  diagnostics?: DiagnosticHealthState
}

type SettingsDraft = {
  llm_do_not_disturb: boolean
  llm_data_consent: boolean
  target_lanlan: string
  card_catalog_network_enabled: boolean
  overlay_enabled: boolean
  overlay_height_percent: number
  overlay_font_size: number
  overlay_speed_px_per_second: number
}

type ActionOutcome = {
  ok: boolean
  refreshed: boolean
  result: Record<string, unknown>
  error?: string
}

const DEFAULT_SETTINGS: SettingsDraft = {
  llm_do_not_disturb: false,
  llm_data_consent: true,
  target_lanlan: "",
  card_catalog_network_enabled: true,
  overlay_enabled: true,
  overlay_height_percent: 32,
  overlay_font_size: 24,
  overlay_speed_px_per_second: 150,
}

function asSettingsDraft(value?: SettingsState): SettingsDraft {
  const dataSharingEnabled = value?.llm_data_consent !== false
  return {
    llm_do_not_disturb: value?.llm_do_not_disturb === true,
    llm_data_consent: dataSharingEnabled,
    target_lanlan: String(value?.target_lanlan || ""),
    card_catalog_network_enabled: value?.card_catalog_network_enabled !== false,
    overlay_enabled: value?.overlay_enabled !== false,
    overlay_height_percent: Number(value?.overlay_height_percent ?? DEFAULT_SETTINGS.overlay_height_percent),
    overlay_font_size: Number(value?.overlay_font_size ?? DEFAULT_SETTINGS.overlay_font_size),
    overlay_speed_px_per_second: Number(
      value?.overlay_speed_px_per_second ?? DEFAULT_SETTINGS.overlay_speed_px_per_second,
    ),
  }
}

function stateTone(value: string): Tone {
  if (["watching", "playing", "running", "healthy", "submitted", "callback_succeeded", "fresh"].includes(value)) return "success"
  if (["degraded", "unavailable", "error", "unhealthy", "failed"].includes(value)) return "danger"
  if (["waiting", "waiting_for_log", "starting", "mulligan", "skipped", "rejected", "not_checked", "never"].includes(value)) return "warning"
  if (["bootstrap_incomplete", "spectator", "ended"].includes(value)) return "info"
  return "default"
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message) return error.message
  if (typeof error === "string" && error) return error
  return fallback
}

function unwrapActionResult(envelope: unknown): Record<string, unknown> {
  if (!envelope || typeof envelope !== "object") return {}
  const value = envelope as Record<string, unknown>
  if (value.result && typeof value.result === "object") {
    return value.result as Record<string, unknown>
  }
  return value
}

export default function HearthstoneCompanionPanel(props: PluginSurfaceProps<DashboardState>) {
  const { actions, state, t } = props
  const safeState = state || {}
  const runtime = safeState.runtime || {}
  const game = safeState.game || {}
  const battlegrounds = game.battlegrounds || {}
  const opponentPlayerId = battlegrounds.current_opponent_player_id
    || battlegrounds.next_opponent_player_id
    || battlegrounds.last_opponent_player_id
  const opponentLabel = battlegrounds.current_opponent_player_id
    ? t("battlegrounds.currentOpponent")
    : battlegrounds.next_opponent_player_id
      ? t("battlegrounds.nextOpponent")
      : t("battlegrounds.lastOpponent")
  const season = safeState.battlegrounds_season || {}
  const seasonStats = safeState.battlegrounds_stats?.seasons?.[String(season.key || "")] || {}
  const statsStorage = safeState.battlegrounds_stats_storage || {}
  const soloStats = seasonStats.solo || {}
  const duosStats = seasonStats.duos || {}
  const overlay = safeState.overlay || {}
  const catalog = safeState.card_catalog || {}
  const catalogDataset = catalog.dataset || {}
  const privacy = safeState.privacy || {}
  const diagnostics = safeState.diagnostics || {}
  const diagnosticSnapshot = diagnostics.snapshot || {}
  const diagnosticLog = diagnostics.log || {}
  const toolRegistration = diagnostics.tool_registration || {}
  const diagnosticRoutes = diagnostics.routes || {}
  const agentDiagnostic = diagnosticRoutes.agent || {}
  const lifecycleDiagnostic = diagnosticRoutes.lifecycle || {}
  const llmToolDiagnostic = diagnosticRoutes.llm_tool || {}
  const player = game.player || {}
  const opponent = game.opponent || {}
  const playerBoard = player.board || {}
  const opponentBoard = opponent.board || {}
  const toast = useToast()
  const confirm = useConfirm()
  const [draft, setDraft] = useState<SettingsDraft>(() => asSettingsDraft(safeState.settings))
  const [draftPatch, setDraftPatch] = useState<Partial<SettingsDraft>>({})
  const draftDirty = Object.keys(draftPatch).length > 0
  const [logPathDraft, setLogPathDraft] = useState(() => String(safeState.settings?.log_path || ""))
  const [logPathDirty, setLogPathDirty] = useState(false)
  const [logPathNotice, setLogPathNotice] = useState("")
  const [logPathFailure, setLogPathFailure] = useState("")
  const [busyAction, setBusyAction] = useState("")
  const [notice, setNotice] = useState("")
  const [failure, setFailure] = useState("")
  const [refreshWarning, setRefreshWarning] = useState("")
  const [diagnosticExport, setDiagnosticExport] = useState<{ path: string; filename: string } | null>(null)
  const [manualRefreshBusy, setManualRefreshBusy] = useState(false)
  const preserveDraftOnCleanRef = useRef(false)
  const preserveLogPathOnCleanRef = useRef(false)
  const apiRef = useRef(props.api)
  const refreshInFlightRef = useRef<Promise<void> | null>(null)
  apiRef.current = props.api

  function trackRefresh(request: Promise<void>): Promise<void> {
    refreshInFlightRef.current = request
    const clear = () => {
      if (refreshInFlightRef.current === request) refreshInFlightRef.current = null
    }
    void request.then(clear, clear)
    return request
  }

  function refreshContext(forceFresh = false): Promise<void> {
    const active = refreshInFlightRef.current
    if (active && !forceFresh) return active
    const request = active
      ? active.catch(() => undefined).then(() => apiRef.current.refresh()).then(() => undefined)
      : Promise.resolve().then(() => apiRef.current.refresh()).then(() => undefined)
    return trackRefresh(request)
  }

  useEffect(() => {
    let cancelled = false
    let timerId: number | undefined
    const refreshLater = async () => {
      try {
        await refreshContext(false)
      } catch {
        // Background refresh is best-effort and must not create recurring alerts.
      }
      if (!cancelled) timerId = window.setTimeout(refreshLater, 500)
    }
    timerId = window.setTimeout(refreshLater, 500)
    return () => {
      cancelled = true
      if (timerId !== undefined) window.clearTimeout(timerId)
    }
  }, [])

  useEffect(() => {
    if (preserveDraftOnCleanRef.current) {
      preserveDraftOnCleanRef.current = false
      return
    }
    setDraft({
      ...asSettingsDraft(safeState.settings),
      ...draftPatch,
    })
  }, [safeState.settings, draftPatch])

  useEffect(() => {
    if (logPathDirty) return
    if (preserveLogPathOnCleanRef.current) {
      preserveLogPathOnCleanRef.current = false
      return
    }
    setLogPathDraft(String(safeState.settings?.log_path || ""))
  }, [safeState.settings, logPathDirty])

  const recentCards = useMemo<RecentCardRow[]>(
    () => [...(game.recent_cards || [])].reverse().map((card, index) => ({
      ...card,
      _key: `${card.turn ?? 0}:${card.side || "unknown"}:${card.card_id || card.card || "card"}:${index}`,
    })),
    [game.recent_cards],
  )
  const lobbyRows = useMemo(
    () => (battlegrounds.lobby || []).map((player) => ({
      ...player,
      _key: String(player.player_id || 0),
    })),
    [battlegrounds.lobby],
  )
  const heroChoiceRows = useMemo(
    () => (battlegrounds.hero_choices || []).map((hero, index) => ({
      ...hero,
      _key: `${hero.card_id || hero.name || "hero-choice"}:${index}`,
    })),
    [battlegrounds.hero_choices],
  )
  const shopRows = useMemo(
    () => (battlegrounds.shop || []).map((card, index) => ({
      ...card,
      _key: `${card.card_id || card.name || "shop"}:${index}`,
    })),
    [battlegrounds.shop],
  )
  const handRows = useMemo(
    () => (battlegrounds.hand || []).map((card, index) => ({
      ...card,
      _key: `${card.card_id || card.name || "hand"}:${index}`,
    })),
    [battlegrounds.hand],
  )
  const warbandRows = useMemo(
    () => (battlegrounds.warband || []).map((card, index) => ({
      ...card,
      _key: `${card.card_id || card.name || "warband"}:${index}`,
    })),
    [battlegrounds.warband],
  )
  const currentChoiceRows = useMemo(
    () => (battlegrounds.current_choice?.options || []).map((card, index) => ({
      ...card,
      _key: `${card.card_id || card.name || "choice"}:${index}`,
    })),
    [battlegrounds.current_choice],
  )
  const seasonMechanics = useMemo(
    () => (season.mechanics || []).map((mechanic, index) => ({
      ...mechanic,
      _key: mechanic.id || String(index),
    })),
    [season.mechanics],
  )

  function actionAvailable(actionId: string): boolean {
    return actions.some((action) => action.id === actionId)
  }

  function localized(prefix: string, value?: string): string {
    const normalized = String(value || "unknown").toLowerCase()
    const known: Record<string, string[]> = {
      "status.source": ["watching", "waiting", "waiting_for_log", "bootstrap_incomplete", "degraded", "stopped", "unknown"],
      "status.phase": ["idle", "starting", "mulligan", "playing", "hero_select", "recruit", "combat", "spectator", "ended", "unknown"],
      "status.mode": ["constructed", "battlegrounds", "unknown"],
      "status.side": ["player", "opponent", "unknown"],
      "status.result": ["won", "placed", "lost", "tied", "conceded", "unknown"],
      "status.overlayReason": ["overlay_disabled", "windows_required", "tkinter_unavailable", "python_probe_failed", "unknown"],
    }
    return known[prefix]?.includes(normalized) ? t(`${prefix}.${normalized}`) : String(value || t("common.unknown"))
  }

  function timestamp(value?: number): string {
    if (!value || value <= 0) return t("common.never")
    return new Date(value * 1000).toLocaleString(props.locale)
  }

  function ageLabel(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return t("common.notAvailable")
    return t("diagnostics.seconds", { value: Math.round(value * 10) / 10 })
  }

  function yesNo(value?: boolean): string {
    if (value == null) return t("common.unknown")
    return value ? t("common.yes") : t("common.no")
  }

  function boardCards(value?: string[]): string {
    return value && value.length > 0 ? value.join(", ") : t("common.none")
  }

  function mana(side: SideState): string {
    if (side.mana_available == null && side.mana_max == null) return t("common.notAvailable")
    return `${side.mana_available ?? 0}/${side.mana_max ?? 0}`
  }

  function percentage(value?: number | null, games?: number): string {
    return games && value != null ? `${value}%` : t("common.insufficientData")
  }

  function numberLabel(value?: number | null): string {
    return value == null ? t("common.unknown") : String(value)
  }

  function battlegroundsStatsLabel(card: BattlegroundsCard): string {
    if ((card.attack == null || card.attack === 0) && card.health == null) return t("common.notAvailable")
    return `${card.attack ?? 0}/${card.health ?? 0}`
  }

  function battlegroundsKeywordsLabel(value?: Record<string, boolean | null>): string {
    const entries = Object.entries(value || {})
    if (!entries.length) return t("common.none")
    return entries
      .sort(([left], [right]) => left.localeCompare(right))
      .map(
        ([keyword, active]) =>
          `${keyword}:${active == null ? t("common.unknown") : active ? t("common.yes") : t("common.no")}`,
      )
      .join(", ")
  }

  function battlegroundsAreaLabel(value?: BattlegroundsArea): string {
    if (!value) return t("common.notAvailable")
    const fragments = [
      yesNo(value.complete),
      value.phase ? localized("status.phase", value.phase) : "",
      value.round ? t("battlegrounds.round", { value: value.round }) : "",
    ].filter(Boolean)
    return fragments.length ? fragments.join(" · ") : t("common.unknown")
  }

  function battlegroundsChoiceCountLabel(value?: BattlegroundsChoice | null): string {
    if (!value) return t("common.unknown")
    const min = Number(value.count_min ?? 0)
    const max = Number(value.count_max ?? 0)
    return min === max ? String(max) : `${min}-${max}`
  }

  function updateDraft(patch: Partial<SettingsDraft>) {
    setDraft((current) => ({ ...current, ...patch }))
    setDraftPatch((current) => ({ ...current, ...patch }))
  }

  async function runAction(
    actionId: string,
    args: Record<string, unknown>,
    successKey: string | ((result: Record<string, unknown>) => string),
    announce = true,
  ): Promise<ActionOutcome> {
    if (!actionAvailable(actionId)) {
      const message = t("errors.actionUnavailable", { action: actionId })
      if (announce) {
        setFailure(message)
        toast.error(message)
      }
      return { ok: false, refreshed: false, result: {}, error: message }
    }
    setBusyAction(actionId)
    if (announce) {
      setFailure("")
      setNotice("")
    }
    setRefreshWarning("")
    try {
      const result = unwrapActionResult(await props.api.call(actionId, args))
      let refreshed = true
      try {
        await refreshContext(true)
      } catch {
        refreshed = false
        const refreshMessage = t("warnings.refreshAfterAction")
        setRefreshWarning(refreshMessage)
        toast.warning(refreshMessage)
      }
      const message = t(typeof successKey === "function" ? successKey(result) : successKey)
      if (announce) {
        setNotice(message)
        toast.success(message)
      }
      return { ok: true, refreshed, result }
    } catch (error) {
      try {
        await refreshContext(true)
      } catch {
        // Preserve the action error; refresh is best-effort on failure.
      }
      const message = errorText(error, t("errors.actionFailed"))
      if (announce) {
        setFailure(message)
        toast.error(message)
      }
      return { ok: false, refreshed: false, result: {}, error: message }
    } finally {
      setBusyAction("")
    }
  }

  async function preparePowerLog() {
    const accepted = await confirm({
      title: t("confirm.prepareLog.title"),
      message: t("confirm.prepareLog.message"),
      tone: "warning",
      confirmLabel: t("confirm.prepareLog.confirm"),
      cancelLabel: t("common.cancel"),
    })
    if (accepted) {
      await runAction(
        "prepare_power_log",
        {},
        (result) => result.changed ? "messages.logPreparedChanged" : "messages.logPreparedReady",
      )
    }
  }

  async function manualRefresh() {
    setManualRefreshBusy(true)
    setFailure("")
    try {
      await refreshContext(true)
    } catch (error) {
      setFailure(errorText(error, t("errors.refreshFailed")))
    } finally {
      setManualRefreshBusy(false)
    }
  }

  async function resetBattlegroundsStats() {
    const accepted = await confirm({
      title: t("confirm.resetStats.title"),
      message: t("confirm.resetStats.message"),
      tone: "danger",
      confirmLabel: t("confirm.resetStats.confirm"),
      cancelLabel: t("common.cancel"),
    })
    if (accepted) await runAction("reset_battlegrounds_stats", { confirm: true }, "messages.statsReset")
  }

  async function saveSettings() {
    const submitted = { ...draftPatch }
    const outcome = await runAction(
      "save_settings",
      submitted,
      (result) => result.lifecycle_enabled === false
        ? "messages.savedLocalOnly"
        : result.do_not_disturb === true
          ? "messages.savedWithDoNotDisturb"
          : "messages.savedWithoutDoNotDisturb",
    )
    if (outcome.ok) {
      preserveDraftOnCleanRef.current = !outcome.refreshed
      setDraftPatch((current) => {
        const remaining = { ...current }
        for (const key of Object.keys(submitted) as (keyof SettingsDraft)[]) {
          if (remaining[key] === submitted[key]) delete remaining[key]
        }
        return remaining
      })
    }
  }

  async function saveLogPath() {
    const normalized = logPathDraft.trim()
    setLogPathNotice("")
    setLogPathFailure("")
    const successKey = normalized ? "messages.logPathSaved" : "messages.logPathAutoDetection"
    const outcome = await runAction("save_settings", { log_path: normalized }, successKey, false)
    if (!outcome.ok) {
      setLogPathFailure(outcome.error || t("errors.actionFailed"))
      return
    }
    preserveLogPathOnCleanRef.current = !outcome.refreshed
    setLogPathDraft(normalized)
    setLogPathDirty(false)
    setLogPathNotice(t(successKey))
  }

  function setConsent(enabled: boolean) {
    updateDraft({ llm_data_consent: enabled })
    setFailure("")
  }

  function setDoNotDisturb(enabled: boolean) {
    updateDraft({ llm_do_not_disturb: enabled })
    setFailure("")
  }

  async function exportDiagnostics() {
    const outcome = await runAction(
      "export_diagnostics",
      {},
      "messages.diagnosticsExported",
    )
    if (!outcome.ok) return
    const path = String(outcome.result.path || "")
    const filename = String(outcome.result.filename || "hearthstone-diagnostics.json")
    if (!path) {
      const message = t("errors.diagnosticsExportEmpty")
      setFailure(message)
      toast.error(message)
      return
    }
    setDiagnosticExport({ path, filename })
  }

  const sourceState = String(runtime.source_state || "unknown")
  const phase = String(game.phase || "unknown")
  const overlayStatus = overlay.running ? "running" : overlay.available === false ? "unavailable" : "stopped"
  const overlaySettingEnabled = draft.overlay_enabled && safeState.settings?.overlay_enabled !== false
  const connectionKey = sourceState === "watching"
    ? "setup.connection.connected"
    : sourceState === "bootstrap_incomplete"
      ? "setup.connection.bootstrap"
      : sourceState === "degraded" || Boolean(runtime.last_error_code)
        ? "setup.connection.degraded"
        : runtime.monitor_running
          ? "setup.connection.waiting"
          : "setup.connection.stopped"
  const enableActionKey = !draft.llm_data_consent
    ? "actions.enable_companion.localOnly"
    : !draft.llm_do_not_disturb
      ? "actions.enable_companion.withoutDoNotDisturb"
      : "actions.enable_companion.withDoNotDisturb"
  const shopArea = battlegrounds.areas?.shop
  const handArea = battlegrounds.areas?.hand
  const warbandArea = battlegrounds.areas?.warband
  const economyArea = battlegrounds.areas?.economy
  const currentChoiceArea = battlegrounds.areas?.choice || battlegrounds.areas?.current_choice
  const sharedBattlegroundsColumns = [
    { key: "name", label: t("battlegroundsShop.card"), render: (row: BattlegroundsCard) => row.name || row.card_id || t("common.unknown") },
    { key: "card_type", label: t("battlegroundsCards.cardType"), render: (row: BattlegroundsCard) => row.card_type || t("common.unknown") },
    { key: "tier", label: t("battlegroundsShop.tier"), render: (row: BattlegroundsCard) => row.tier || t("common.unknown") },
    { key: "attack", label: t("battlegroundsShop.stats"), render: (row: BattlegroundsCard) => battlegroundsStatsLabel(row) },
    { key: "current_cost", label: t("battlegroundsCards.cost"), render: (row: BattlegroundsCard) => numberLabel(row.current_cost) },
    { key: "premium", label: t("battlegroundsCards.premium"), render: (row: BattlegroundsCard) => yesNo(row.premium ?? undefined) },
    { key: "keywords", label: t("battlegroundsCards.keywords"), render: (row: BattlegroundsCard) => battlegroundsKeywordsLabel(row.keywords) },
  ]
  const shopColumns = [
    { key: "position", label: t("battlegroundsShop.position"), render: (row: BattlegroundsCard) => row.position || t("common.unknown") },
    ...sharedBattlegroundsColumns,
    { key: "frozen", label: t("battlegroundsShop.frozen"), render: (row: BattlegroundsCard) => yesNo(row.frozen) },
  ]
  const rosterColumns = [
    { key: "position", label: t("battlegroundsShop.position"), render: (row: BattlegroundsCard) => row.position || t("common.unknown") },
    ...sharedBattlegroundsColumns,
  ]

  return (
    <Page title={t("panel.title")} subtitle={t("panel.subtitle")}>
      <Stack>
        <Inline justify="space-between" align="center" wrap>
          <Inline align="center" wrap>
            <StatusBadge tone={stateTone(sourceState)} label={localized("status.source", sourceState)} />
            <StatusBadge tone={stateTone(phase)} label={localized("status.phase", phase)} />
          </Inline>
          <Button disabled={manualRefreshBusy} onClick={manualRefresh}>
            {t("actions.refresh.label")}
          </Button>
        </Inline>

        {notice ? <Alert tone="success">{notice}</Alert> : null}
        {refreshWarning ? <Warning>{refreshWarning}</Warning> : null}
        {failure ? <InlineError title={t("errors.title")} error={failure} /> : null}
        {props.warnings?.length ? (
          <Warning>{t("warnings.hosted", { count: props.warnings.length })}</Warning>
        ) : null}

        <Card title={t("sections.setup.title")}>
          <Stack>
            <Alert tone="info">{t("setup.offlineHelp")}</Alert>
            <Inline align="center" wrap>
              <Text>{t("setup.connection.label")}</Text>
              <StatusBadge tone={stateTone(sourceState)} label={localized("status.source", sourceState)} />
              <Text>{t(connectionKey)}</Text>
            </Inline>
            <Divider />
            <Heading as="h3">{t("setup.consent.title")}</Heading>
            <Switch
              checked={draft.llm_data_consent}
              label={t("settings.llmConsent")}
              onChange={setConsent}
            />
            <Text>{t("setup.consent.help")}</Text>
            <Heading as="h3">{t("setup.doNotDisturb.title")}</Heading>
            <Switch
              checked={draft.llm_do_not_disturb}
              label={t("settings.llmDoNotDisturb")}
              onChange={setDoNotDisturb}
            />
            <Text>{t("setup.doNotDisturb.help")}</Text>
            <Divider />
            <Inline align="center" wrap>
              {draftDirty ? <StatusBadge tone="warning" label={t("setup.pending")} /> : null}
              <Button
                tone="success"
                disabled={Boolean(busyAction) || !actionAvailable("save_settings")}
                onClick={saveSettings}
              >
                {t(enableActionKey)}
              </Button>
            </Inline>
          </Stack>
        </Card>

        {game.mode === "battlegrounds" ? (
          <Stack>
            <Card title={t("sections.battlegrounds.title")}>
              <Stack>
                <Inline align="center" wrap>
                  <StatusBadge tone={stateTone(String(battlegrounds.phase || phase))} label={localized("status.phase", battlegrounds.phase || phase)} />
                  <StatusBadge tone="info" label={t(`battlegrounds.variant.${battlegrounds.variant || "solo"}`)} />
                  <Text>{t("battlegrounds.round", { value: battlegrounds.round ?? 0 })}</Text>
                  {battlegrounds.placement ? <StatusBadge tone="info" label={t("battlegrounds.placement", { value: battlegrounds.placement })} /> : null}
                </Inline>
                <Grid cols={4}>
                  <StatCard label={t("battlegrounds.gold")} value={battlegrounds.gold == null ? t("common.notAvailable") : `${battlegrounds.gold}/${battlegrounds.max_gold ?? "?"}`} />
                  <StatCard label={t("battlegrounds.tavernTier")} value={battlegrounds.tavern_tier ?? 0} />
                  <StatCard label={t("battlegrounds.warbandSize")} value={battlegrounds.warband?.length ?? 0} />
                  <StatCard label={opponentLabel} value={opponentPlayerId || t("common.unknown")} />
                </Grid>
                <KeyValue
                  items={[
                    { key: "frozen", label: t("battlegrounds.shopFrozen"), value: yesNo(battlegrounds.frozen) },
                    { key: "refresh_cost", label: t("battlegrounds.refreshCost"), value: numberLabel(battlegrounds.refresh_cost ?? battlegrounds.economy?.refresh_cost) },
                    { key: "upgrade_cost", label: t("battlegrounds.upgradeCost"), value: numberLabel(battlegrounds.upgrade_cost ?? battlegrounds.economy?.upgrade_cost) },
                    { key: "shop_area", label: t("battlegrounds.shopObserved"), value: battlegroundsAreaLabel(shopArea) },
                    { key: "hand_area", label: t("battlegrounds.handObserved"), value: battlegroundsAreaLabel(handArea) },
                    { key: "warband_area", label: t("battlegrounds.warbandObserved"), value: battlegroundsAreaLabel(warbandArea) },
                    { key: "economy_area", label: t("battlegrounds.economyObserved"), value: battlegroundsAreaLabel(economyArea) },
                    { key: "source", label: t("battlegrounds.source"), value: t("battlegrounds.powerLogSource") },
                  ]}
                />
              </Stack>
            </Card>

            {(battlegrounds.phase || phase) === "hero_select" || heroChoiceRows.length > 0 ? (
              <Card title={t("sections.battlegroundsHeroChoices.title")}>
                <Stack>
                  <Text>{t("battlegroundsHeroChoices.observedHelp")}</Text>
                  {heroChoiceRows.length ? (
                    <DataTable
                      data={heroChoiceRows}
                      rowKey="_key"
                      maxRows={8}
                      emptyText={t("battlegroundsHeroChoices.empty")}
                      columns={[
                        { key: "name", label: t("battlegroundsHeroChoices.hero"), render: (row) => row.name || row.card_id || t("common.unknown") },
                        { key: "card_id", label: t("battlegroundsHeroChoices.cardId"), render: (row) => row.card_id || t("common.unknown") },
                      ]}
                    />
                  ) : (
                    <EmptyState title={t("battlegroundsHeroChoices.empty")} description={t("battlegroundsHeroChoices.emptyHelp")} />
                  )}
                </Stack>
              </Card>
            ) : null}

            <Card title={t("sections.battlegroundsLobby.title")}>
              {lobbyRows.length ? (
                <DataTable
                  data={lobbyRows}
                  rowKey="_key"
                  maxRows={8}
                  emptyText={t("battlegroundsLobby.empty")}
                  columns={[
                    { key: "player_id", label: t("battlegroundsLobby.player") },
                    { key: "hero_name", label: t("battlegroundsLobby.hero"), render: (row) => row.hero_name || row.hero_card_id || t("common.unknown") },
                    { key: "health", label: t("battlegroundsLobby.health"), render: (row) => row.health == null ? t("common.unknown") : `${row.health}+${row.armor ?? 0}` },
                    { key: "tavern_tier", label: t("battlegroundsLobby.tier") },
                    { key: "placement", label: t("battlegroundsLobby.place"), render: (row) => row.placement || t("common.unknown") },
                    { key: "next_opponent", label: t("battlegroundsLobby.status"), render: (row) => row.is_local ? t("battlegroundsLobby.local") : row.is_teammate ? t("battlegroundsLobby.teammate") : row.current_opponent ? t("battlegroundsLobby.current") : row.next_opponent ? t("battlegroundsLobby.next") : row.last_opponent ? t("battlegroundsLobby.last") : row.eliminated ? t("battlegroundsLobby.eliminated") : t("battlegroundsLobby.alive") },
                    { key: "last_seen_round", label: t("battlegroundsLobby.observed"), render: (row) => row.last_seen_round ? t("battlegroundsLobby.observedRound", { value: row.last_seen_round }) : t("common.none") },
                  ]}
                />
              ) : (
                <EmptyState title={t("battlegroundsLobby.empty")} description={t("battlegroundsLobby.emptyHelp")} />
              )}
            </Card>

            {battlegrounds.current_choice || currentChoiceRows.length > 0 ? (
              <Card title={t("sections.battlegroundsChoice.title")}>
                <Stack>
                  <KeyValue
                    items={[
                      { key: "type", label: t("battlegroundsChoice.type"), value: battlegrounds.current_choice?.choice_type || t("common.unknown") },
                      { key: "count", label: t("battlegroundsChoice.count"), value: battlegroundsChoiceCountLabel(battlegrounds.current_choice) },
                      { key: "source", label: t("battlegroundsChoice.source"), value: battlegrounds.current_choice?.source?.name || battlegrounds.current_choice?.source?.card_id || t("common.none") },
                      { key: "observed", label: t("battlegroundsChoice.observed"), value: battlegroundsAreaLabel(currentChoiceArea) },
                    ]}
                  />
                  {currentChoiceRows.length ? (
                    <DataTable
                      data={currentChoiceRows}
                      rowKey="_key"
                      maxRows={8}
                      emptyText={t("battlegroundsChoice.empty")}
                      columns={rosterColumns}
                    />
                  ) : (
                    <EmptyState title={t("battlegroundsChoice.empty")} description={t("battlegroundsChoice.emptyHelp")} />
                  )}
                </Stack>
              </Card>
            ) : null}

            <Card title={t("sections.battlegroundsShop.title")}>
              {shopRows.length ? (
                <DataTable
                  data={shopRows}
                  rowKey="_key"
                  maxRows={10}
                  emptyText={t("battlegroundsShop.empty")}
                  columns={shopColumns}
                />
              ) : (
                <EmptyState title={t("battlegroundsShop.empty")} description={t("battlegroundsShop.emptyHelp")} />
              )}
            </Card>

            <Card title={t("sections.battlegroundsHand.title")}>
              {handRows.length ? (
                <DataTable
                  data={handRows}
                  rowKey="_key"
                  maxRows={10}
                  emptyText={t("battlegroundsHand.empty")}
                  columns={rosterColumns}
                />
              ) : (
                <EmptyState title={t("battlegroundsHand.empty")} description={t("battlegroundsHand.emptyHelp")} />
              )}
            </Card>

            <Card title={t("sections.battlegroundsWarband.title")}>
              {warbandRows.length ? (
                <DataTable
                  data={warbandRows}
                  rowKey="_key"
                  maxRows={10}
                  emptyText={t("battlegroundsWarband.empty")}
                  columns={rosterColumns}
                />
              ) : (
                <EmptyState title={t("battlegroundsWarband.empty")} description={t("battlegroundsWarband.emptyHelp")} />
              )}
            </Card>

            <Card title={t("sections.battlegroundsStats.title")}>
              <Stack>
                <Alert tone="info">{t("battlegroundsStats.localOnly", { patch: season.patch || t("common.unknown") })}</Alert>
                {statsStorage.degraded ? (
                  <InlineError
                    title={t("battlegroundsStats.storageError")}
                    message={t("battlegroundsStats.storageErrorHelp")}
                    details={statsStorage.error_code || t("common.unknown")}
                  />
                ) : null}
                <Heading as="h3">{t("battlegroundsStats.solo")}</Heading>
                <Grid cols={4}>
                  <StatCard label={t("battlegroundsStats.games")} value={soloStats.games ?? 0} />
                  <StatCard label={t("battlegroundsStats.top4Rate")} value={percentage(soloStats.top4_rate, soloStats.games)} />
                  <StatCard label={t("battlegroundsStats.firstRate")} value={percentage(soloStats.first_rate, soloStats.games)} />
                  <StatCard label={t("battlegroundsStats.averagePlace")} value={soloStats.average_placement ?? t("common.insufficientData")} />
                </Grid>
                <Divider />
                <Heading as="h3">{t("battlegroundsStats.duos")}</Heading>
                <Grid cols={4}>
                  <StatCard label={t("battlegroundsStats.games")} value={duosStats.games ?? 0} />
                  <StatCard label={t("battlegroundsStats.top2Rate")} value={percentage(duosStats.top2_rate, duosStats.games)} />
                  <StatCard label={t("battlegroundsStats.firstRate")} value={percentage(duosStats.first_rate, duosStats.games)} />
                  <StatCard label={t("battlegroundsStats.averagePlace")} value={duosStats.average_placement ?? t("common.insufficientData")} />
                </Grid>
                <Button
                  tone="danger"
                  disabled={Boolean(busyAction) || !actionAvailable("reset_battlegrounds_stats")}
                  onClick={resetBattlegroundsStats}
                >
                  {t("actions.reset_battlegrounds_stats.label")}
                </Button>
              </Stack>
            </Card>

            <Card title={t("sections.battlegroundsSeason.title")}>
              <Stack>
                <KeyValue
                  items={[
                    { key: "season", label: t("battlegroundsSeason.season"), value: season.season ?? t("common.unknown") },
                    { key: "name", label: t("battlegroundsSeason.name"), value: season.name || t("common.unknown") },
                    { key: "patch", label: t("battlegroundsSeason.patch"), value: season.patch || t("common.unknown") },
                    { key: "verified", label: t("battlegroundsSeason.verified"), value: season.verified_at || t("common.unknown") },
                  ]}
                />
                <Warning>{t("battlegroundsSeason.staticNotice")}</Warning>
                <DataTable
                  data={seasonMechanics}
                  rowKey="_key"
                  maxRows={8}
                  emptyText={t("common.none")}
                  columns={[
                    { key: "title", label: t("battlegroundsSeason.mechanic") },
                    { key: "summary", label: t("battlegroundsSeason.summary") },
                  ]}
                />
                <Text>{season.source_url || t("common.none")}</Text>
              </Stack>
            </Card>
          </Stack>
        ) : null}

        {game.mode !== "battlegrounds" ? (
          <>
        <Card title={t("sections.game.title")}>
          <Stack>
            <Inline align="center" wrap>
              <StatusBadge tone={stateTone(phase)} label={localized("status.phase", phase)} />
              <Text>{t("game.number", { value: game.game_number ?? 0 })}</Text>
              <Text>{t("game.round", { value: game.round ?? 0 })}</Text>
              <Text>{t("game.actionTurn", { value: game.turn ?? 0 })}</Text>
              <Text>{t("game.activeSide", { side: localized("status.side", game.active_side) })}</Text>
              {game.result ? <StatusBadge tone="info" label={localized("status.result", game.result)} /> : null}
            </Inline>
            <Grid cols={2}>
              <Stack>
                <Heading as="h3">{t("game.player")}</Heading>
                <KeyValue
                  items={[
                    { key: "health", label: t("game.health"), value: player.health ?? t("common.notAvailable") },
                    { key: "armor", label: t("game.armor"), value: player.armor ?? 0 },
                    { key: "effectiveHealth", label: t("game.effectiveHealth"), value: player.effective_health ?? t("common.notAvailable") },
                    { key: "mana", label: t("game.mana"), value: mana(player) },
                    { key: "hand", label: t("game.hand"), value: player.hand_count ?? 0 },
                    { key: "deck", label: t("game.deck"), value: player.deck_count ?? 0 },
                    { key: "secrets", label: t("game.secrets"), value: player.secret_count ?? 0 },
                    { key: "boardCount", label: t("game.boardCount"), value: playerBoard.count ?? 0 },
                    { key: "boardStats", label: t("game.boardStats"), value: `${playerBoard.attack ?? 0}/${playerBoard.health ?? 0}` },
                    { key: "boardCards", label: t("game.boardCards"), value: boardCards(playerBoard.cards) },
                  ]}
                />
              </Stack>
              <Stack>
                <Heading as="h3">{t("game.opponent")}</Heading>
                <KeyValue
                  items={[
                    { key: "health", label: t("game.health"), value: opponent.health ?? t("common.notAvailable") },
                    { key: "armor", label: t("game.armor"), value: opponent.armor ?? 0 },
                    { key: "effectiveHealth", label: t("game.effectiveHealth"), value: opponent.effective_health ?? t("common.notAvailable") },
                    { key: "mana", label: t("game.mana"), value: mana(opponent) },
                    { key: "hand", label: t("game.hand"), value: opponent.hand_count ?? 0 },
                    { key: "deck", label: t("game.deck"), value: opponent.deck_count ?? 0 },
                    { key: "secrets", label: t("game.secrets"), value: opponent.secret_count ?? 0 },
                    { key: "boardCount", label: t("game.boardCount"), value: opponentBoard.count ?? 0 },
                    { key: "boardStats", label: t("game.boardStats"), value: `${opponentBoard.attack ?? 0}/${opponentBoard.health ?? 0}` },
                    { key: "boardCards", label: t("game.boardCards"), value: boardCards(opponentBoard.cards) },
                  ]}
                />
              </Stack>
            </Grid>
          </Stack>
        </Card>

        <Card title={t("sections.recentCards.title")}>
          {recentCards.length > 0 ? (
            <DataTable
              data={recentCards}
              rowKey="_key"
              emptyText={t("recentCards.empty")}
              columns={[
                { key: "turn", label: t("recentCards.turn") },
                {
                  key: "side",
                  label: t("recentCards.side"),
                  render: (row) => localized("status.side", row.side),
                },
                { key: "card", label: t("recentCards.card") },
                { key: "card_id", label: t("recentCards.cardId"), render: (row) => row.card_id || t("common.none") },
              ]}
            />
          ) : (
            <EmptyState title={t("recentCards.empty")} description={t("recentCards.emptyHelp")} />
          )}
        </Card>
          </>
        ) : null}

        <Card title={t("sections.settings.title")}>
          <Stack>
            <Switch
              checked={draft.card_catalog_network_enabled}
              label={t("settings.cardCatalogNetwork")}
              onChange={(value) => updateDraft({ card_catalog_network_enabled: value })}
            />
            <Text>{t("settings.cardCatalogNetworkHelp")}</Text>
            <Divider />
            <Switch
              checked={draft.overlay_enabled}
              label={t("settings.overlayEnabled")}
              onChange={(value) => updateDraft({ overlay_enabled: value })}
            />
            <Grid cols={3}>
              <Field label={t("settings.overlayHeight")} help={t("settings.overlayHeightHelp")}>
                <Slider
                  value={draft.overlay_height_percent}
                  min={15}
                  max={80}
                  step={1}
                  showValue
                  onChange={(value) => updateDraft({ overlay_height_percent: value })}
                />
              </Field>
              <Field label={t("settings.overlayFontSize")} help={t("settings.overlayFontSizeHelp")}>
                <Slider
                  value={draft.overlay_font_size}
                  min={14}
                  max={48}
                  step={1}
                  showValue
                  onChange={(value) => updateDraft({ overlay_font_size: value })}
                />
              </Field>
              <Field label={t("settings.overlaySpeed")} help={t("settings.overlaySpeedHelp")}>
                <Slider
                  value={draft.overlay_speed_px_per_second}
                  min={60}
                  max={360}
                  step={10}
                  showValue
                  onChange={(value) => updateDraft({ overlay_speed_px_per_second: value })}
                />
              </Field>
            </Grid>
            <Divider />
            <Field label={t("settings.targetLanlan")} help={t("settings.targetLanlanHelp")}>
              <Input
                value={draft.target_lanlan}
                placeholder={t("settings.targetLanlanPlaceholder")}
                onChange={(value) => updateDraft({ target_lanlan: value })}
              />
            </Field>
            <Button
              tone="primary"
              disabled={Boolean(busyAction) || !actionAvailable("save_settings")}
              onClick={saveSettings}
            >
              {t("actions.save_settings.label")}
            </Button>
          </Stack>
        </Card>

        <Card title={t("sections.privacy.title")}>
          <Stack>
            <Alert tone={privacy.llm_public_state_sharing_enabled ? "warning" : "success"}>
              {privacy.llm_public_state_sharing_enabled ? t("privacy.sharingEnabled") : t("privacy.sharingDisabled")}
            </Alert>
            <KeyValue
              items={[
                { key: "rawLog", label: t("privacy.rawLogUploaded"), value: yesNo(privacy.raw_log_uploaded) },
                { key: "names", label: t("privacy.playerNamesRetained"), value: yesNo(privacy.player_names_retained) },
                { key: "hidden", label: t("privacy.hiddenCardsExposed"), value: yesNo(privacy.hidden_opponent_cards_exposed) },
                { key: "sharing", label: t("privacy.publicStateSharing"), value: yesNo(privacy.llm_public_state_sharing_enabled) },
                { key: "lifecycle", label: t("privacy.lifecycleReactions"), value: yesNo(privacy.llm_lifecycle_reactions_enabled) },
                { key: "doNotDisturb", label: t("privacy.doNotDisturb"), value: yesNo(safeState.settings?.llm_do_not_disturb) },
                { key: "catalogNetwork", label: t("privacy.cardCatalogNetwork"), value: yesNo(privacy.card_catalog_network_enabled) },
                { key: "catalogState", label: t("privacy.cardCatalogGameState"), value: yesNo(privacy.card_catalog_sends_game_state) },
              ]}
            />
            <Text>{t("privacy.disclosure")}</Text>
          </Stack>
        </Card>

        <Divider />
        <Heading as="h2">{t("sections.diagnostics.title")}</Heading>
        <Text>{t("sections.diagnostics.subtitle")}</Text>
        <Grid cols={3}>
          <StatCard label={t("metrics.round")} value={game.round ?? 0} />
          <StatCard label={t("metrics.events")} value={runtime.events_seen ?? 0} />
          <StatCard label={t("metrics.llmSubmissions")} value={runtime.llm_submissions ?? 0} />
        </Grid>
        <Card title={t("sections.deliveryDiagnostics.title")}>
          <Stack>
            <Inline>
              <StatusBadge
                tone={stateTone(diagnosticLog.fresh ? "fresh" : "unavailable")}
                label={diagnosticLog.fresh ? t("diagnostics.logFresh") : t("diagnostics.logNotFresh")}
              />
              <StatusBadge
                tone={stateTone(String(toolRegistration.status || "not_checked"))}
                label={t("diagnostics.toolRegistrationStatus", { value: toolRegistration.status || "not_checked" })}
              />
            </Inline>
            <KeyValue
              items={[
                { key: "mode", label: t("diagnostics.mode"), value: localized("status.mode", diagnosticSnapshot.mode) },
                { key: "phase", label: t("diagnostics.phase"), value: localized("status.phase", diagnosticSnapshot.phase) },
                { key: "round", label: t("diagnostics.round"), value: diagnosticSnapshot.round ?? 0 },
                { key: "generation", label: t("diagnostics.sourceGeneration"), value: diagnosticSnapshot.source_generation ?? 0 },
                { key: "revision", label: t("diagnostics.snapshotRevision"), value: diagnosticSnapshot.revision ?? 0 },
                { key: "lineAge", label: t("diagnostics.lineAge"), value: ageLabel(diagnosticLog.line_age_seconds) },
                { key: "stateAge", label: t("diagnostics.stateAge"), value: ageLabel(diagnosticLog.state_age_seconds) },
                { key: "toolReason", label: t("diagnostics.toolRegistrationReason"), value: toolRegistration.reason || t("common.none") },
                { key: "missingTools", label: t("diagnostics.missingTools"), value: toolRegistration.missing?.join(", ") || t("common.none") },
                { key: "lastTool", label: t("diagnostics.lastTool"), value: `${llmToolDiagnostic.status || "never"} · ${llmToolDiagnostic.mode || "-"} · ${llmToolDiagnostic.focus || "-"}` },
                { key: "lastToolReason", label: t("diagnostics.lastToolReason"), value: llmToolDiagnostic.reason || t("common.none") },
                { key: "lastAgent", label: t("diagnostics.lastAgent"), value: `${agentDiagnostic.status || "never"} · ${agentDiagnostic.mode || "-"} · ${agentDiagnostic.focus || "-"}` },
                { key: "lastAgentReason", label: t("diagnostics.lastAgentReason"), value: agentDiagnostic.reason || t("common.none") },
                { key: "lastLifecycle", label: t("diagnostics.lastLifecycle"), value: `${lifecycleDiagnostic.status || "never"} · ${lifecycleDiagnostic.mode || "-"} · ${lifecycleDiagnostic.focus || "-"}` },
                { key: "lastLifecycleReason", label: t("diagnostics.lastLifecycleReason"), value: lifecycleDiagnostic.reason || t("common.none") },
              ]}
            />
            {!diagnosticLog.fresh && diagnosticSnapshot.game_number ? (
              <Warning>{t("diagnostics.logNotFreshHelp")}</Warning>
            ) : null}
            {toolRegistration.status === "unhealthy" ? (
              <InlineError
                title={t("diagnostics.toolRegistrationUnhealthy")}
                details={toolRegistration.error_code || toolRegistration.reason || t("common.unknown")}
              />
            ) : null}
            <Button
              tone="info"
              disabled={Boolean(busyAction) || !actionAvailable("export_diagnostics")}
              onClick={exportDiagnostics}
            >
              {t("actions.export_diagnostics.label")}
            </Button>
            {diagnosticExport ? (
              <FileDownload
                path={diagnosticExport.path}
                filename={diagnosticExport.filename}
                label={t("actions.open_diagnostics.label")}
                tone="default"
              />
            ) : null}
          </Stack>
        </Card>
        <Grid cols={2}>
          <Card title={t("sections.runtime.title")}>
            <Stack>
              <KeyValue
                items={[
                  { key: "monitor", label: t("runtime.monitor"), value: runtime.monitor_running ? t("common.running") : t("common.stopped") },
                  { key: "source", label: t("runtime.source"), value: localized("status.source", sourceState) },
                  { key: "path", label: t("runtime.path"), value: runtime.resolved_log_path || t("common.notDetected") },
                  { key: "lines", label: t("runtime.lines"), value: runtime.lines_seen ?? 0 },
                  { key: "lastLine", label: t("runtime.lastLine"), value: timestamp(runtime.last_line_at) },
                  { key: "lastEvent", label: t("runtime.lastEvent"), value: timestamp(runtime.last_event_at) },
                  { key: "eventKind", label: t("runtime.lastEventKind"), value: runtime.last_event_kind || t("common.none") },
                  { key: "catalogAvailable", label: t("catalog.available"), value: yesNo(catalog.available) },
                  { key: "catalogProvider", label: t("catalog.provider"), value: catalogDataset.provider || t("common.none") },
                  { key: "catalogPatch", label: t("catalog.patch"), value: catalogDataset.patch || t("common.unknown") },
                  { key: "catalogCards", label: t("catalog.cards"), value: catalog.card_count ?? 0 },
                  { key: "catalogChecked", label: t("catalog.checked"), value: timestamp(catalogDataset.checked_at) },
                  { key: "catalogStale", label: t("catalog.stale"), value: yesNo(catalogDataset.stale) },
                ]}
              />
              {catalogDataset.stale ? <Warning>{t("catalog.staleWarning")}</Warning> : null}
              {catalog.degraded_reason ? (
                <Warning>{t("catalog.degraded", { reason: catalog.degraded_reason })}</Warning>
              ) : null}
              {runtime.last_error_code ? (
                <InlineError
                  title={t("runtime.lastError")}
                  message={t("runtime.lastErrorHelp")}
                  details={runtime.last_error_code}
                />
              ) : null}
              <Divider />
              <Field label={t("settings.logPath")} help={t("settings.logPathHelp")}>
                <Input
                  value={logPathDraft}
                  placeholder={t("settings.logPathPlaceholder")}
                  onChange={(value) => {
                    setLogPathDraft(value)
                    setLogPathDirty(true)
                    setLogPathNotice("")
                    setLogPathFailure("")
                  }}
                />
              </Field>
              {logPathNotice ? <Alert tone="success">{logPathNotice}</Alert> : null}
              {logPathFailure ? <InlineError title={t("errors.title")} error={logPathFailure} /> : null}
              <Button
                tone="primary"
                disabled={Boolean(busyAction) || !logPathDirty || !actionAvailable("save_settings")}
                onClick={saveLogPath}
              >
                {logPathDraft.trim() ? t("actions.save_log_path.label") : t("actions.restore_auto_log_path.label")}
              </Button>
              <ButtonGroup>
                <Button
                  tone="success"
                  disabled={Boolean(busyAction) || Boolean(runtime.monitor_running) || !actionAvailable("start_monitoring")}
                  onClick={async () => { await runAction("start_monitoring", {}, "messages.monitorStarted") }}
                >
                  {t("actions.start_monitoring.label")}
                </Button>
                <Button
                  tone="danger"
                  disabled={Boolean(busyAction) || !runtime.monitor_running || !actionAvailable("stop_monitoring")}
                  onClick={async () => { await runAction("stop_monitoring", {}, "messages.monitorStopped") }}
                >
                  {t("actions.stop_monitoring.label")}
                </Button>
                <Button
                  tone="primary"
                  disabled={Boolean(busyAction) || !actionAvailable("prepare_power_log")}
                  onClick={preparePowerLog}
                >
                  {t("actions.prepare_power_log.label")}
                </Button>
              </ButtonGroup>
            </Stack>
          </Card>

          <Card title={t("sections.overlay.title")}>
            <Stack>
              <StatusBadge tone={stateTone(overlayStatus)} label={t(`status.overlay.${overlayStatus}`)} />
              <KeyValue
                items={[
                  { key: "available", label: t("overlay.available"), value: yesNo(overlay.available) },
                  { key: "running", label: t("overlay.running"), value: overlay.running ? t("common.running") : t("common.stopped") },
                  { key: "pid", label: t("overlay.pid"), value: overlay.pid ?? t("common.none") },
                  {
                    key: "reason",
                    label: t("overlay.reason"),
                    value: overlay.reason ? localized("status.overlayReason", overlay.reason) : t("common.none"),
                  },
                ]}
              />
              {overlay.available === false ? <Warning>{t("overlay.unavailableHelp")}</Warning> : null}
              <ButtonGroup>
                <Button
                  tone="success"
                  disabled={Boolean(busyAction) || !overlaySettingEnabled || overlay.available === false || Boolean(overlay.running) || !actionAvailable("start_overlay")}
                  onClick={async () => { await runAction("start_overlay", {}, "messages.overlayStarted") }}
                >
                  {t("actions.start_overlay.label")}
                </Button>
                <Button
                  tone="danger"
                  disabled={Boolean(busyAction) || !overlay.running || !actionAvailable("stop_overlay")}
                  onClick={async () => { await runAction("stop_overlay", {}, "messages.overlayStopped") }}
                >
                  {t("actions.stop_overlay.label")}
                </Button>
                <Button
                  tone="info"
                  disabled={Boolean(busyAction) || !actionAvailable("test_commentary")}
                  onClick={async () => {
                    await runAction(
                      "test_commentary",
                      {},
                      (result) => result.llm_submitted
                        ? "messages.testCharacterSubmitted"
                        : "messages.testOverlaySubmitted",
                    )
                  }}
                >
                  {t("actions.test_commentary.label")}
                </Button>
              </ButtonGroup>
            </Stack>
          </Card>
        </Grid>
      </Stack>
    </Page>
  )
}
