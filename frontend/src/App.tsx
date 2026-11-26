import { FormEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";

const CONVERSATION_TOKEN_HEADER = "X-Conversation-Token";

function tabIdentity(key: string, prefix: string): string {
  const existing = sessionStorage.getItem(key);
  if (existing) return existing;
  const value = `${prefix}-${crypto.randomUUID()}`;
  sessionStorage.setItem(key, value);
  return value;
}

const DEMO_USER = tabIdentity("clip-demo-user", "web-demo");
const DEMO_SESSION = tabIdentity("clip-demo-session", "browser");

type Mode = "baseline" | "agent";
type LlmProvider = "ollama" | "openai";
type Outcome = "answered" | "clarification_required" | "quality_review_required" |
  "conflict_detected" | "out_of_scope" | "invalid_input" | "technical_error";
type FeedbackRating = "helpful" | "incomplete" | "incorrect" | "missing_documents";

type Source = {
  chunk_id: string;
  document_id: string;
  title: string;
  filename: string;
  page: number | null;
  parent_text: string;
  child_text: string;
  child_start: number;
  child_end: number;
};

type DocumentItem = {
  id: string;
  title: string;
  filename: string;
  active: boolean;
  status: string;
  chunks: number;
};

type Analytics = {
  total_documents: number;
  active_documents: number;
  total_queries: number;
  total_conversations: number;
  user_messages: number;
  clarifications: number;
  confirmed_answers: number;
  quality_review_transitions: number;
  technical_errors: number;
  justified_abstentions: number;
  manual_review_required: number;
  average_latency_ms: number;
  p95_latency_ms: number | null;
  latency_measurement: "measured" | "not_measured";
  llm_calls: number;
  input_tokens: number;
  output_tokens: number;
  tokens_measurement: "measured" | "not_measured";
  modes: {baseline: number; agent: number; baseline_share: number; agent_share: number};
  fallback_count: number;
  answer_correctness: "not_measured";
  time_series: Array<{
    date: string;
    queries: number;
    technical_errors: number;
    manual_review_required: number;
  }>;
};

type TraceSummary = {
  id: string;
  question: string;
  user_id: string;
  session_id: string;
  mode: Mode;
  status: string;
  technical_status: "success" | "error";
  quality_status: string;
  outcome: Outcome | null;
  reason_code: string | null;
  categories: string[];
  latency_ms: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  retrieved_count: number;
  selected_count: number;
  details: Record<string, unknown>;
  created_at: string;
};

type TrajectoryEvent = {
  step: number;
  node: string;
  decision: string;
  latency_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  [key: string]: unknown;
};

type TraceDetail = TraceSummary & {
  answer: string | null;
  sources: Source[];
  feedback: Array<{id: string; rating: FeedbackRating; created_at: string}>;
};

type AskMetrics = {
  latency_ms?: number | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  agent_enabled?: boolean;
  citations_valid?: boolean;
  llm_provider?: LlmProvider;
  llm_model?: string;
};

type AskResult = {
  answer: string;
  sources: Source[];
  trace_id: string;
  outcome: Outcome;
  reason_code: string | null;
  llm_provider: LlmProvider;
  llm_model: string;
  metrics: AskMetrics;
  conversation_id?: string;
  turn_id?: string;
  conversation_status?: ConversationStatus;
  awaiting_clarification?: boolean;
  expires_at?: string;
};

type ConversationStatus = "processing" | "awaiting_clarification" | "completed" |
  "closed" | "expired";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  kind: string;
  content: string;
  trace_id?: string | null;
  outcome?: Outcome | null;
  sources: Source[];
  llm_provider?: LlmProvider | null;
  llm_model?: string | null;
  metrics?: AskMetrics;
};

type ConversationHistory = {
  conversation_id: string;
  status: ConversationStatus;
  pending_clarification: string | null;
  expires_at: string;
  turns: Array<ChatMessage & {ordinal: number}>;
};

type LlmProviderCapability = {
  id: LlmProvider;
  label: string;
  enabled: boolean;
  configured: boolean;
  available: boolean;
  default_model: string;
  models: string[];
};

type Capabilities = {
  default_mode: Mode;
  agent_demo_override_enabled: boolean;
  llm: {
    default_provider: LlmProvider;
    providers: LlmProviderCapability[];
  };
};

const defaultQuestion =
  "Какие пени применяются за просрочку предоплаты и почему в документах указаны разные ставки?";

const categoryLabels: Record<string, string> = {
  technical_error: "Техническая ошибка",
  no_evidence: "Нет найденных доказательств",
  evidence_gap: "Пробел в доказательствах",
  justified_abstention: "Обоснованный отказ",
  citation_problem: "Проблема цитирования",
  agent_fallback: "Fallback агента",
  manual_review_required: "Нужна проверка",
  answer_produced: "Ответ сформирован",
};

const feedbackLabels: Record<FeedbackRating, string> = {
  helpful: "Полезный",
  incomplete: "Неполный",
  incorrect: "Неправильный",
  missing_documents: "Не хватает документов",
};

const outcomeLabels: Record<Outcome, string> = {
  answered: "Подтверждённый ответ",
  clarification_required: "Нужно уточнение",
  quality_review_required: "Недостаточно подтверждённой информации",
  conflict_detected: "Обнаружено противоречие",
  out_of_scope: "Запрос вне компетенции",
  invalid_input: "Некорректный запрос",
  technical_error: "Техническая ошибка",
};

function formatNumber(value: number | null | undefined): string {
  return value == null ? "не измерено" : new Intl.NumberFormat("ru-RU").format(value);
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {dateStyle: "short", timeStyle: "medium"}).format(
    new Date(value),
  );
}

async function responseError(response: Response): Promise<string> {
  try {
    const payload = await response.json();
    const detail = payload.detail;
    return typeof detail === "string" ? detail : "Запрос не выполнен";
  } catch {
    return "Запрос не выполнен";
  }
}

function ModeBadge({mode}: {mode: Mode}) {
  return <span className={`mode mode-${mode}`}>
    {mode === "agent" ? "AI-Агент" : "Базовый RAG"}
  </span>;
}

function AnswerText({text, sourceCount, anchorPrefix = "source"}: {
  text: string;
  sourceCount: number;
  anchorPrefix?: string;
}) {
  const parts = text.split(/(\[\d+\]|\*\*[^*]+\*\*)/g);
  return <p className="answer-copy">{parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={`${part}-${index}`}>{part.slice(2, -2)}</strong>;
    }
    const match = /^\[(\d+)\]$/.exec(part);
    if (!match) return <span key={`${part}-${index}`}>{part}</span>;
    const sourceNumber = Number(match[1]);
    if (sourceNumber < 1 || sourceNumber > sourceCount) {
      return <span className="invalid-citation" key={`${part}-${index}`}>{part}</span>;
    }
    return <a className="citation" href={`#${anchorPrefix}-${sourceNumber}`} key={`${part}-${index}`}>{part}</a>;
  })}</p>;
}

function SourceCard({source, index, anchorPrefix = "source"}: {
  source: Source;
  index: number;
  anchorPrefix?: string;
}) {
  const start = Math.max(0, Math.min(source.child_start, source.parent_text.length));
  const end = Math.max(start, Math.min(source.child_end, source.parent_text.length));
  return <article className="source-card" id={`${anchorPrefix}-${index + 1}`}>
    <div className="source-heading">
      <div><span className="source-number">[{index + 1}]</span><h3>{source.title}</h3></div>
      {source.page != null && <span className="page">стр. {source.page}</span>}
    </div>
    <small>{source.filename}</small>
    <p className="child-excerpt">{source.child_text}</p>
    <details>
      <summary>Показать родительский контекст</summary>
      <p className="parent-context">
        {source.parent_text.slice(0, start)}
        <mark>{source.parent_text.slice(start, end)}</mark>
        {source.parent_text.slice(end)}
      </p>
    </details>
  </article>;
}

function FeedbackForm({traceId, userId = DEMO_USER, sessionId = DEMO_SESSION, onSaved}: {
  traceId: string;
  userId?: string;
  sessionId?: string;
  onSaved?: () => void;
}) {
  const [saved, setSaved] = useState<FeedbackRating | null>(null);
  const [pending, setPending] = useState<FeedbackRating | null>(null);
  const [error, setError] = useState("");

  async function submit(rating: FeedbackRating) {
    setPending(rating);
    setError("");
    try {
      const response = await fetch(`/api/v1/traces/${traceId}/feedback`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({rating, user_id: userId, session_id: sessionId}),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setSaved(rating);
      onSaved?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Оценка не сохранена");
    } finally {
      setPending(null);
    }
  }

  return <div className="feedback">
    <strong>Оценить ответ</strong>
    <span className="muted">Оценка помогает анализу, но не подтверждает истинность фактов.</span>
    <div className="feedback-actions">{(Object.keys(feedbackLabels) as FeedbackRating[]).map((rating) =>
      <button
        className={saved === rating ? "selected" : "secondary"}
        disabled={pending != null}
        key={rating}
        onClick={() => submit(rating)}
        type="button"
      >{pending === rating ? "Сохраняю…" : feedbackLabels[rating]}</button>,
    )}</div>
    {error && <span className="inline-error">{error}</span>}
  </div>;
}

function MetricCard({value, label, note}: {value: ReactNode; label: string; note?: string}) {
  return <div className="metric-card"><b>{value}</b><span>{label}</span>{note && <small>{note}</small>}</div>;
}

function SearchView({capabilities}: {capabilities: Capabilities}) {
  const [question, setQuestion] = useState(defaultQuestion);
  const [requestedMode, setRequestedMode] = useState<Mode>(capabilities.default_mode);
  const [llmProvider, setLlmProvider] = useState<LlmProvider>(capabilities.llm.default_provider);
  const initialProvider = capabilities.llm.providers.find(
    (provider) => provider.id === capabilities.llm.default_provider,
  ) ?? capabilities.llm.providers[0];
  const [llmModel, setLlmModel] = useState(initialProvider.default_model);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState(
    () => sessionStorage.getItem("clip-conversation-id"),
  );
  const [conversationToken, setConversationToken] = useState(
    () => sessionStorage.getItem("clip-conversation-token"),
  );
  const [conversationStatus, setConversationStatus] = useState<ConversationStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lastSubmission = useRef("");

  useEffect(() => setRequestedMode(capabilities.default_mode), [capabilities.default_mode]);
  useEffect(() => {
    setLlmProvider(capabilities.llm.default_provider);
    const selected = capabilities.llm.providers.find(
      (provider) => provider.id === capabilities.llm.default_provider,
    );
    if (selected) setLlmModel(selected.default_model);
  }, [capabilities]);

  useEffect(() => {
    if (!conversationId || !conversationToken) return;
    setBusy(true);
    fetch(`/api/v1/conversations/${conversationId}`, {
      headers: {[CONVERSATION_TOKEN_HEADER]: conversationToken},
    }).then(async (response) => {
      if (!response.ok) throw new Error(await responseError(response));
      const history = await response.json() as ConversationHistory;
      setMessages(history.turns.map((turn) => ({
        ...turn,
        id: turn.id,
        sources: turn.sources ?? [],
      })));
      setConversationStatus(history.status);
      if (history.status === "awaiting_clarification") {
        setQuestion("");
        setRequestedMode("agent");
      }
    }).catch((reason) => {
      sessionStorage.removeItem("clip-conversation-id");
      sessionStorage.removeItem("clip-conversation-token");
      setConversationId(null);
      setConversationToken(null);
      setConversationStatus(null);
      setError(reason instanceof Error ? reason.message : "Диалог не восстановлен");
    }).finally(() => setBusy(false));
  }, []);

  const selectedProvider = capabilities.llm.providers.find(
    (provider) => provider.id === llmProvider,
  ) ?? initialProvider;

  function changeProvider(provider: LlmProvider) {
    setLlmProvider(provider);
    const selected = capabilities.llm.providers.find((item) => item.id === provider);
    if (selected) setLlmModel(selected.default_model);
  }

  async function submitQuestion(event: FormEvent) {
    event.preventDefault();
    const content = question.trim();
    if (!content) return;
    const submissionKey = `${conversationId ?? "new"}:${content}`;
    if (busy || submissionKey === lastSubmission.current) return;
    lastSubmission.current = submissionKey;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/v1/ask", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(conversationToken ? {[CONVERSATION_TOKEN_HEADER]: conversationToken} : {}),
        },
        body: JSON.stringify({
          question: content,
          user_id: DEMO_USER,
          session_id: DEMO_SESSION,
          llm_provider: llmProvider,
          llm_model: llmModel,
          ...(conversationId ? {conversation_id: conversationId} : {}),
          ...(capabilities.agent_demo_override_enabled ? {mode: requestedMode} : {}),
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = await response.json() as AskResult;
      const token = response.headers.get(CONVERSATION_TOKEN_HEADER) ?? conversationToken;
      if (result.conversation_id && token) {
        sessionStorage.setItem("clip-conversation-id", result.conversation_id);
        sessionStorage.setItem("clip-conversation-token", token);
        setConversationId(result.conversation_id);
        setConversationToken(token);
      }
      if (result.conversation_status) setConversationStatus(result.conversation_status);
      if (result.awaiting_clarification) setRequestedMode("agent");
      setMessages((current) => [
        ...current,
        {id: crypto.randomUUID(), role: "user", kind: conversationId ? "clarification_answer" : "original_question", content, sources: []},
        {
          id: result.turn_id ?? crypto.randomUUID(),
          role: "assistant",
          kind: result.outcome === "clarification_required" ? "clarification_question" : "final_answer",
          content: result.answer,
          trace_id: result.trace_id,
          outcome: result.outcome,
          sources: result.sources,
          llm_provider: result.llm_provider,
          llm_model: result.llm_model,
          metrics: result.metrics,
        },
      ]);
      setQuestion("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Поиск не выполнен");
      lastSubmission.current = "";
    } finally {
      setBusy(false);
    }
  }

  async function closeCurrentDialogue() {
    if (!conversationId || !conversationToken) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`/api/v1/conversations/${conversationId}/close`, {
        method: "POST",
        headers: {[CONVERSATION_TOKEN_HEADER]: conversationToken},
      });
      if (!response.ok) throw new Error(await responseError(response));
      setConversationStatus("closed");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Диалог не завершён");
    } finally {
      setBusy(false);
    }
  }

  async function newDialogue() {
    if (conversationId && conversationToken && conversationStatus === "awaiting_clarification") {
      await fetch(`/api/v1/conversations/${conversationId}/close`, {
        method: "POST",
        headers: {[CONVERSATION_TOKEN_HEADER]: conversationToken},
      }).catch(() => undefined);
    }
    sessionStorage.removeItem("clip-conversation-id");
    sessionStorage.removeItem("clip-conversation-token");
    setConversationId(null);
    setConversationToken(null);
    setConversationStatus(null);
    setMessages([]);
    setQuestion("");
    setError("");
    lastSubmission.current = "";
  }

  const awaiting = conversationStatus === "awaiting_clarification";
  const canSubmit = !busy && conversationStatus !== "closed" && conversationStatus !== "completed";
  return <section className="stack">
    <form className="panel ask" onSubmit={submitQuestion}>
      <div className="form-heading">
        <div><span className="section-kicker">Диалог с доказательствами</span><h2>{awaiting ? "Ответьте на уточнение" : "Вопрос по документам"}</h2></div>
        <div className="request-controls">
          <label className="mode-select">Языковая модель
            <select value={llmProvider} onChange={(event) => changeProvider(event.target.value as LlmProvider)}>
              {capabilities.llm.providers.map((provider) => <option
                disabled={!provider.available}
                key={provider.id}
                value={provider.id}
              >{provider.label}{!provider.available ? " — недоступен" : ""}</option>)}
            </select>
          </label>
          {selectedProvider.models.length > 1 && <label className="mode-select">Модель
            <select value={llmModel} onChange={(event) => setLlmModel(event.target.value)}>
              {selectedProvider.models.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
          </label>}
          {capabilities.agent_demo_override_enabled ? <label className="mode-select">Режим
            <select disabled={awaiting} value={requestedMode} onChange={(event) => setRequestedMode(event.target.value as Mode)}>
              <option value="baseline">Базовый RAG</option>
              <option value="agent">AI-Агент</option>
            </select>
          </label> : <ModeBadge mode={capabilities.default_mode} />}
        </div>
      </div>
      {capabilities.agent_demo_override_enabled && <p className="notice compact">
        Локальный demo override включён конфигурацией сервера. Он действует только для этого запроса.
      </p>}
      <label htmlFor="question">{awaiting ? "Ваше уточнение" : "Сформулируйте вопрос"}</label>
      <textarea id="question" maxLength={1000} required value={question} onChange={(event) => {
        setQuestion(event.target.value);
        lastSubmission.current = "";
      }} />
      <div className="dialog-actions">
        <button className="primary wide" disabled={!canSubmit}>{busy ? "Обрабатываю…" : awaiting ? "Продолжить" : "Найти ответ"}</button>
        {conversationId && <button className="secondary" onClick={newDialogue} type="button">Новый диалог</button>}
        {awaiting && <button className="secondary danger" disabled={busy} onClick={closeCurrentDialogue} type="button">Завершить</button>}
      </div>
    </form>
    {error && <div className="error">{error}</div>}
    {conversationStatus === "closed" && <div className="notice">Диалог завершён без дополнительных LLM-вызовов. Начните новый диалог, чтобы задать вопрос.</div>}
    <div className="dialogue">{messages.map((message) => {
      const prefix = `message-${message.id}`;
      if (message.role === "user") return <article className="chat-message user-message" key={message.id}>
        <span className="section-kicker">Вы</span><p>{message.content}</p>
      </article>;
      return <article className={`chat-message assistant-message ${message.kind === "clarification_question" ? "clarification-message" : ""}`} key={message.id}>
        <div className="answer-header"><div><span className="section-kicker">AI-Агент</span><h3>{message.outcome ? outcomeLabels[message.outcome] : "Ответ"}</h3></div></div>
        {message.outcome && <div className={`outcome outcome-${message.outcome}`}>{outcomeLabels[message.outcome]}</div>}
        <AnswerText text={message.content} sourceCount={message.sources.length} anchorPrefix={prefix} />
        {message.outcome === "answered" && <p className="notice compact">Ответ прошёл автоматическую проверку по найденным источникам, но не является экспертным утверждением.</p>}
        {message.metrics && <div className="answer-meta">
          <span>{formatNumber(message.metrics.latency_ms)} мс</span>
          <span>{formatNumber(message.metrics.prompt_tokens)} input tokens</span>
          <span>{formatNumber(message.metrics.completion_tokens)} output tokens</span>
          {message.llm_provider && <span>{message.llm_provider === "openai" ? "OpenAI" : "Ollama"} · {message.llm_model}</span>}
        </div>}
        {message.trace_id && <FeedbackForm traceId={message.trace_id} userId={DEMO_USER} sessionId={DEMO_SESSION} />}
        {message.sources.length > 0 && <details className="message-sources"><summary>Источники: {message.sources.length}</summary><div className="sources">{message.sources.map((source, index) => <SourceCard source={source} index={index} anchorPrefix={prefix} key={source.chunk_id} />)}</div></details>}
      </article>;
    })}</div>
  </section>;
}

function AnalyticsView({analytics}: {analytics: Analytics | null}) {
  const maxQueries = Math.max(1, ...(analytics?.time_series.map((item) => item.queries) ?? [1]));
  if (!analytics) return <div className="panel empty">Загружаю аналитику…</div>;
  return <section className="stack">
    <div className="notice">Технический успех не означает правильность ответа. Содержательная корректность: <b>не измерена</b>.</div>
    <div className="metric-grid">
      <MetricCard value={analytics.total_conversations} label="Диалогов" />
      <MetricCard value={analytics.user_messages} label="Сообщений пользователя" />
      <MetricCard value={analytics.clarifications} label="Уточнений" />
      <MetricCard value={analytics.confirmed_answers} label="Подтверждённых ответов" />
      <MetricCard value={analytics.quality_review_transitions} label="Переходов в службу качества" />
      <MetricCard value={analytics.total_queries} label="Всего запросов" />
      <MetricCard value={analytics.technical_errors} label="Технических ошибок" />
      <MetricCard value={analytics.justified_abstentions} label="Обоснованных отказов" />
      <MetricCard value={analytics.manual_review_required} label="Требуют проверки" />
      <MetricCard value={analytics.latency_measurement === "measured" ? `${formatNumber(analytics.average_latency_ms)} мс` : "не измерено"} label="Средняя latency" />
      <MetricCard value={analytics.p95_latency_ms == null ? "не измерено" : `${formatNumber(analytics.p95_latency_ms)} мс`} label="p95 latency" />
      <MetricCard value={analytics.llm_calls} label="LLM-вызовов" />
      <MetricCard value={analytics.fallback_count} label="Fallback" />
      <MetricCard value={analytics.tokens_measurement === "measured" ? formatNumber(analytics.input_tokens) : "не измерено"} label="Input tokens" />
      <MetricCard value={analytics.tokens_measurement === "measured" ? formatNumber(analytics.output_tokens) : "не измерено"} label="Output tokens" />
      <MetricCard value={`${Math.round(analytics.modes.baseline_share * 100)}%`} label="Базовый RAG" note={`${analytics.modes.baseline} запросов`} />
      <MetricCard value={`${Math.round(analytics.modes.agent_share * 100)}%`} label="AI-Агент" note={`${analytics.modes.agent} запросов`} />
    </div>
    <article className="panel chart-panel">
      <div className="section-title"><div><span className="section-kicker">Динамика</span><h2>Запросы по дням</h2></div></div>
      {analytics.time_series.length === 0 ? <div className="empty">История запросов пока пуста.</div> :
        <div className="bars">{analytics.time_series.map((point) => <div className="bar-row" key={point.date}>
          <time>{point.date}</time><div className="bar-track"><span style={{width: `${(point.queries / maxQueries) * 100}%`}} /></div>
          <b>{point.queries}</b><small>{point.technical_errors} ошибок · {point.manual_review_required} на проверку</small>
        </div>)}</div>}
    </article>
  </section>;
}

function TraceViewer({trace, onFeedback}: {trace: TraceDetail; onFeedback: () => void}) {
  const trajectory = Array.isArray(trace.details.trajectory) ? trace.details.trajectory as TrajectoryEvent[] : [];
  const queries = Array.isArray(trace.details.search_queries) ? trace.details.search_queries as string[] : [];
  const gaps = Array.isArray(trace.details.gaps) ? trace.details.gaps : [];
  const validation = trace.details.validation as Record<string, unknown> | undefined;
  const conversationId = typeof trace.details.conversation_id === "string" ? trace.details.conversation_id : null;
  const conversationTurn = typeof trace.details.conversation_turn === "number" ? trace.details.conversation_turn : null;
  return <div className="trace-detail stack">
    <article className="panel">
      <div className="trace-heading"><div><span className="section-kicker">Trace {trace.id}</span><h2>{trace.question}</h2></div><ModeBadge mode={trace.mode} /></div>
      <div className="trace-meta"><span>{formatDate(trace.created_at)}</span><span>user: {trace.user_id}</span><span>session: {trace.session_id}</span><span>status: {trace.status}</span></div>
      {conversationId && <p className="muted">Диалог: {conversationId} · ход: {conversationTurn ?? "—"}. Токен доступа и чужая история здесь не отображаются.</p>}
      {trace.outcome && <div className={`outcome outcome-${trace.outcome}`}>{outcomeLabels[trace.outcome]}</div>}
      <div className="chips">{trace.categories.map((category) => <span key={category}>{categoryLabels[category] ?? category}</span>)}</div>
      {trace.answer ? <AnswerText text={trace.answer} sourceCount={trace.sources.length} /> : <p className="muted">Ответ не сформирован.</p>}
      <div className="answer-meta"><span>{formatNumber(trace.latency_ms)} мс</span><span>{formatNumber(trace.prompt_tokens)} input</span><span>{formatNumber(trace.completion_tokens)} output</span></div>
      <FeedbackForm traceId={trace.id} userId={trace.user_id} sessionId={trace.session_id} onSaved={onFeedback} />
      {trace.feedback.length > 0 && <p className="muted">Оценок: {trace.feedback.length}. Они не используются как подтверждение факта.</p>}
    </article>
    <article className="panel">
      <span className="section-kicker">Execution</span><h2>{trace.mode === "agent" ? "Траектория AI-Агента" : "Выполнение Базового RAG"}</h2>
      {trace.mode === "baseline" ? <p className="notice compact">Это ответ Базового RAG. AI-Агент для этого trace не запускался.</p> : trajectory.length === 0 ?
        <p className="empty">Траектория не записана.</p> : <ol className="timeline">{trajectory.map((event) => {
          const {step, node, decision, latency_ms, prompt_tokens, completion_tokens, ...details} = event;
          return <li key={`${step}-${node}`}><div className="timeline-dot">{step}</div><details open={step === 1}>
            <summary><b>{node}</b><span>{decision}</span><small>{formatNumber(latency_ms)} мс · {formatNumber(prompt_tokens)} / {formatNumber(completion_tokens)} tokens</small></summary>
            {Object.keys(details).length > 0 && <pre>{JSON.stringify(details, null, 2)}</pre>}
          </details></li>;
        })}</ol>}
      {queries.length > 0 && <div className="trace-block"><h3>Поисковые подзапросы</h3><ol>{queries.map((query, index) => <li key={`${query}-${index}`}>{query}</li>)}</ol></div>}
      {gaps.length > 0 && <div className="trace-block warning"><h3>Evidence gaps</h3><pre>{JSON.stringify(gaps, null, 2)}</pre></div>}
      {validation && <div className="trace-block"><h3>Результат проверки</h3><pre>{JSON.stringify(validation, null, 2)}</pre></div>}
      {Boolean(trace.details.agent_fallback_reason) && <div className="notice danger">Agent fallback: {String(trace.details.agent_fallback_reason)}</div>}
      {Array.isArray(trace.details.errors) && trace.details.errors.length > 0 && <div className="trace-block warning"><h3>Ошибки узлов</h3><pre>{JSON.stringify(trace.details.errors, null, 2)}</pre></div>}
    </article>
    <div className="section-title"><div><span className="section-kicker">Evidence</span><h2>Найденные доказательства</h2></div><span>{trace.sources.length}</span></div>
    <div className="sources">{trace.sources.map((source, index) => <SourceCard source={source} index={index} key={source.chunk_id} />)}</div>
  </div>;
}

function TracesView() {
  const [rows, setRows] = useState<TraceSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [mode, setMode] = useState("");
  const [category, setCategory] = useState("");
  const [selected, setSelected] = useState<TraceDetail | null>(null);
  const [error, setError] = useState("");
  const limit = 10;

  async function loadRows(nextOffset = offset) {
    const params = new URLSearchParams({limit: String(limit), offset: String(nextOffset)});
    if (mode) params.set("mode", mode);
    if (category) params.set("category", category);
    const response = await fetch(`/api/v1/admin/traces?${params}`);
    if (!response.ok) throw new Error(await responseError(response));
    setRows(await response.json());
    setTotal(Number(response.headers.get("X-Total-Count") ?? 0));
    setOffset(nextOffset);
  }

  async function loadDetail(id: string) {
    const response = await fetch(`/api/v1/admin/traces/${id}`);
    if (!response.ok) throw new Error(await responseError(response));
    setSelected(await response.json());
  }

  useEffect(() => { loadRows(0).catch((reason) => setError(String(reason))); }, [mode, category]);

  return <section className="trace-layout">
    <aside className="trace-list panel">
      <div className="filter-grid">
        <label>Режим<select value={mode} onChange={(event) => setMode(event.target.value)}><option value="">Все</option><option value="baseline">Базовый RAG</option><option value="agent">AI-Агент</option></select></label>
        <label>Результат<select value={category} onChange={(event) => setCategory(event.target.value)}><option value="">Все</option>{Object.entries(categoryLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </div>
      {error && <div className="error compact">{error}</div>}
      <div className="trace-items">{rows.map((trace) => <button className={selected?.id === trace.id ? "trace-item selected" : "trace-item"} key={trace.id} onClick={() => loadDetail(trace.id).catch((reason) => setError(String(reason)))}>
        <span><ModeBadge mode={trace.mode} /><time>{formatDate(trace.created_at)}</time></span><b>{trace.question}</b><small>{trace.latency_ms ?? "—"} мс · {trace.categories.map((item) => categoryLabels[item] ?? item).join(", ")}</small>
      </button>)}</div>
      {rows.length === 0 && <div className="empty">Подходящих traces нет.</div>}
      <div className="pagination"><button className="secondary" disabled={offset === 0} onClick={() => loadRows(Math.max(0, offset - limit))}>Назад</button><span>{total === 0 ? 0 : offset + 1}–{Math.min(offset + rows.length, total)} из {total}</span><button className="secondary" disabled={offset + limit >= total} onClick={() => loadRows(offset + limit)}>Далее</button></div>
    </aside>
    <div>{selected ? <TraceViewer trace={selected} onFeedback={() => loadDetail(selected.id)} /> : <div className="panel empty hero-empty">Выберите запрос, чтобы увидеть ответ, доказательства и фактический маршрут исполнения.</div>}</div>
  </section>;
}

function DocumentsView({documents, busy, reload}: {documents: DocumentItem[]; busy: boolean; reload: () => Promise<void>}) {
  const [localBusy, setLocalBusy] = useState(false);
  const [error, setError] = useState("");
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setLocalBusy(true);
    setError("");
    const response = await fetch("/api/v1/documents", {method: "POST", body: new FormData(form)});
    if (!response.ok) setError(await responseError(response)); else {form.reset(); await reload();}
    setLocalBusy(false);
  }
  async function toggle(document: DocumentItem) {
    const action = document.active ? "deactivate" : "restore";
    const response = await fetch(`/api/v1/documents/${document.id}/${action}`, {method: "POST"});
    if (!response.ok) setError(await responseError(response)); else await reload();
  }
  return <section className="stack">
    <form className="panel upload" onSubmit={upload}><div><span className="section-kicker">Корпус</span><h2>Загрузить документ</h2></div><input name="title" placeholder="Название (необязательно)" /><input name="file" type="file" accept=".txt,.md,.pdf,.docx" required /><button disabled={localBusy}>{localBusy ? "Индексирую…" : "Загрузить"}</button></form>
    {error && <div className="error">{error}</div>}
    <div className="panel registry">{busy ? <div className="empty">Загружаю реестр…</div> : documents.map((document) => <div key={document.id}><span><b>{document.title}</b><small>{document.filename} · {document.status} · фрагментов: {document.chunks}</small></span><button className="secondary" onClick={() => toggle(document)}>{document.active ? "Деактивировать" : "Восстановить"}</button></div>)}</div>
  </section>;
}

export default function App() {
  const [tab, setTab] = useState<"search" | "analytics" | "traces" | "documents">("search");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [documentsBusy, setDocumentsBusy] = useState(false);
  const [globalError, setGlobalError] = useState("");

  async function loadAnalytics() {
    const response = await fetch("/api/v1/admin/analytics");
    if (!response.ok) throw new Error(await responseError(response));
    setAnalytics(await response.json());
  }
  async function loadDocuments() {
    setDocumentsBusy(true);
    try {
      const response = await fetch("/api/v1/documents");
      if (!response.ok) throw new Error(await responseError(response));
      setDocuments(await response.json());
    } finally {
      setDocumentsBusy(false);
    }
  }

  useEffect(() => {
    fetch("/api/v1/capabilities").then(async (response) => {
      if (!response.ok) throw new Error(await responseError(response));
      setCapabilities(await response.json());
    }).catch((reason) => setGlobalError(reason instanceof Error ? reason.message : String(reason)));
  }, []);
  useEffect(() => {
    setGlobalError("");
    const request = tab === "analytics" ? loadAnalytics() : tab === "documents" ? loadDocuments() : null;
    request?.catch((reason) => setGlobalError(reason instanceof Error ? reason.message : String(reason)));
  }, [tab]);

  const title = useMemo(() => ({search: "Поиск", analytics: "Аналитика", traces: "Трассы", documents: "Документы"})[tab], [tab]);
  return <main>
    <header>
      <div><span className="eyebrow">Corporate Intelligence Lab</span><h1>AI-Agent Clip</h1><p>{title}</p></div>
      <nav>{(["search", "analytics", "traces", "documents"] as const).map((item) => <button className={tab === item ? "active" : ""} key={item} onClick={() => setTab(item)}>{{search: "Поиск", analytics: "Аналитика", traces: "Трассы", documents: "Документы"}[item]}</button>)}</nav>
    </header>
    {globalError && <div className="error">{globalError}</div>}
    {tab === "search" && (capabilities ? <SearchView capabilities={capabilities} /> : <div className="panel empty">Загружаю фактическую конфигурацию backend…</div>)}
    {tab === "analytics" && <AnalyticsView analytics={analytics} />}
    {tab === "traces" && <TracesView />}
    {tab === "documents" && <DocumentsView documents={documents} busy={documentsBusy} reload={loadDocuments} />}
    <footer>Local-first MVP · общий Basic Auth на публичном стенде · без ACL</footer>
  </main>;
}
