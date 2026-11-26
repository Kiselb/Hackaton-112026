import { FormEvent, ReactNode, useEffect, useMemo, useState } from "react";

const DEMO_USER = "web-demo";
const DEMO_SESSION = "browser";

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

function AnswerText({text, sourceCount}: {text: string; sourceCount: number}) {
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
    return <a className="citation" href={`#source-${sourceNumber}`} key={`${part}-${index}`}>{part}</a>;
  })}</p>;
}

function SourceCard({source, index}: {source: Source; index: number}) {
  const start = Math.max(0, Math.min(source.child_start, source.parent_text.length));
  const end = Math.max(start, Math.min(source.child_end, source.parent_text.length));
  return <article className="source-card" id={`source-${index + 1}`}>
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
  const [result, setResult] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => setRequestedMode(capabilities.default_mode), [capabilities.default_mode]);
  useEffect(() => {
    setLlmProvider(capabilities.llm.default_provider);
    const selected = capabilities.llm.providers.find(
      (provider) => provider.id === capabilities.llm.default_provider,
    );
    if (selected) setLlmModel(selected.default_model);
  }, [capabilities]);

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
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const response = await fetch("/api/v1/ask", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          question,
          user_id: DEMO_USER,
          session_id: DEMO_SESSION,
          llm_provider: llmProvider,
          llm_model: llmModel,
          ...(capabilities.agent_demo_override_enabled ? {mode: requestedMode} : {}),
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setResult(await response.json());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Поиск не выполнен");
    } finally {
      setBusy(false);
    }
  }

  const actualMode: Mode = result?.metrics.agent_enabled ? "agent" : "baseline";
  return <section className="stack">
    <form className="panel ask" onSubmit={submitQuestion}>
      <div className="form-heading">
        <div><span className="section-kicker">Поиск с доказательствами</span><h2>Вопрос по документам</h2></div>
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
            <select value={requestedMode} onChange={(event) => setRequestedMode(event.target.value as Mode)}>
              <option value="baseline">Базовый RAG</option>
              <option value="agent">AI-Агент</option>
            </select>
          </label> : <ModeBadge mode={capabilities.default_mode} />}
        </div>
      </div>
      {capabilities.agent_demo_override_enabled && <p className="notice compact">
        Локальный demo override включён конфигурацией сервера. Он действует только для этого запроса.
      </p>}
      <label htmlFor="question">Сформулируйте вопрос</label>
      <textarea id="question" required value={question} onChange={(event) => setQuestion(event.target.value)} />
      <button className="primary wide" disabled={busy}>{busy ? "Формирую ответ…" : "Найти ответ"}</button>
    </form>
    {error && <div className="error">{error}</div>}
    {result && <>
      <article className="panel answer">
        <div className="answer-header"><div><span className="section-kicker">Результат</span><h2>{outcomeLabels[result.outcome]}</h2></div><ModeBadge mode={actualMode} /></div>
        <div className={`outcome outcome-${result.outcome}`}>{outcomeLabels[result.outcome]}</div>
        <AnswerText text={result.answer} sourceCount={result.sources.length} />
        {result.outcome === "answered" && <p className="notice compact">
          Ответ прошёл автоматическую проверку по найденным источникам, но не является экспертным утверждением.
        </p>}
        {result.sources.length === 0 && result.outcome === "quality_review_required" && <p className="notice compact">
          Поиск не нашёл достаточных доказательств в активных документах. Это не доказывает отсутствие информации во всём корпоративном каталоге.
        </p>}
        <div className="answer-meta">
          <span>{formatNumber(result.metrics.latency_ms)} мс</span>
          <span>{formatNumber(result.metrics.prompt_tokens)} input tokens</span>
          <span>{formatNumber(result.metrics.completion_tokens)} output tokens</span>
          <span>{result.llm_provider === "openai" ? "OpenAI" : "Ollama"} · {result.llm_model}</span>
          <span>{result.metrics.citations_valid ? "Цитаты проверены синтаксически" : "Цитаты требуют проверки"}</span>
        </div>
        <FeedbackForm traceId={result.trace_id} />
      </article>
      <div className="section-title"><div><span className="section-kicker">Evidence</span><h2>Источники</h2></div><span>{result.sources.length}</span></div>
      <div className="sources">{result.sources.map((source, index) => <SourceCard source={source} index={index} key={source.chunk_id} />)}</div>
    </>}
  </section>;
}

function AnalyticsView({analytics}: {analytics: Analytics | null}) {
  const maxQueries = Math.max(1, ...(analytics?.time_series.map((item) => item.queries) ?? [1]));
  if (!analytics) return <div className="panel empty">Загружаю аналитику…</div>;
  return <section className="stack">
    <div className="notice">Технический успех не означает правильность ответа. Содержательная корректность: <b>не измерена</b>.</div>
    <div className="metric-grid">
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
  return <div className="trace-detail stack">
    <article className="panel">
      <div className="trace-heading"><div><span className="section-kicker">Trace {trace.id}</span><h2>{trace.question}</h2></div><ModeBadge mode={trace.mode} /></div>
      <div className="trace-meta"><span>{formatDate(trace.created_at)}</span><span>user: {trace.user_id}</span><span>session: {trace.session_id}</span><span>status: {trace.status}</span></div>
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
