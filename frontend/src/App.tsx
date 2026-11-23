import { FormEvent, useEffect, useState } from "react";

type Source = {
  chunk_id: string;
  document_id: string;
  title: string;
  filename: string;
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
  failed_queries: number;
  average_latency_ms: number;
};

const defaultQuestion =
  "Какие пени применяются за просрочку предоплаты и почему в документах указаны разные ставки?";

function Highlight({ source }: { source: Source }) {
  const before = source.parent_text.slice(0, source.child_start);
  const selected = source.parent_text.slice(source.child_start, source.child_end);
  const after = source.parent_text.slice(source.child_end);
  return <p>{before}<mark>{selected}</mark>{after}</p>;
}

export default function App() {
  const [tab, setTab] = useState<"search" | "admin">("search");
  const [question, setQuestion] = useState(defaultQuestion);
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [metrics, setMetrics] = useState<Record<string, number | null>>({});
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function loadAdmin() {
    const [documentsResponse, analyticsResponse] = await Promise.all([
      fetch("/api/v1/documents"),
      fetch("/api/v1/admin/analytics")
    ]);
    setDocuments(await documentsResponse.json());
    setAnalytics(await analyticsResponse.json());
  }

  useEffect(() => {
    if (tab === "admin") loadAdmin().catch((reason) => setError(String(reason)));
  }, [tab]);

  async function submitQuestion(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/v1/ask", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({question, user_id: "web-demo", session_id: "browser"})
      });
      if (!response.ok) throw new Error(await response.text());
      const data = await response.json();
      setAnswer(data.answer);
      setSources(data.sources);
      setMetrics(data.metrics);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/v1/documents", {method: "POST", body: data});
      if (!response.ok) throw new Error(await response.text());
      event.currentTarget.reset();
      await loadAdmin();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function toggle(document: DocumentItem) {
    const action = document.active ? "deactivate" : "restore";
    const response = await fetch(`/api/v1/documents/${document.id}/${action}`, {method: "POST"});
    if (!response.ok) setError(await response.text());
    await loadAdmin();
  }

  return (
    <main>
      <header>
        <div><span className="eyebrow">Corporate Intelligence Lab</span><h1>AI-Agent Clip</h1></div>
        <nav>
          <button className={tab === "search" ? "active" : ""} onClick={() => setTab("search")}>Поиск</button>
          <button className={tab === "admin" ? "active" : ""} onClick={() => setTab("admin")}>Администрирование</button>
        </nav>
      </header>
      {error && <div className="error">{error}</div>}
      {tab === "search" ? (
        <section>
          <form className="ask" onSubmit={submitQuestion}>
            <label htmlFor="question">Вопрос по корпоративным документам</label>
            <textarea id="question" value={question} onChange={(event) => setQuestion(event.target.value)} />
            <button disabled={busy}>{busy ? "Ищу локально…" : "Найти ответ"}</button>
          </form>
          {answer && <article className="answer"><h2>Ответ</h2><p>{answer}</p>
            <div className="metrics">{Object.entries(metrics).map(([key, value]) => <span key={key}>{key}: {value ?? "n/a"}</span>)}</div>
          </article>}
          <div className="sources">{sources.map((source, index) =>
            <article key={source.chunk_id}><h3>[{index + 1}] {source.title}</h3><small>{source.filename}</small><Highlight source={source} /></article>
          )}</div>
        </section>
      ) : (
        <section>
          <div className="cards">
            <div><b>{analytics?.active_documents ?? 0}</b><span>активных документов</span></div>
            <div><b>{analytics?.total_queries ?? 0}</b><span>запросов</span></div>
            <div><b>{analytics?.average_latency_ms ?? 0}</b><span>мс в среднем</span></div>
            <div><b>{analytics?.failed_queries ?? 0}</b><span>ошибок</span></div>
          </div>
          <form className="upload" onSubmit={upload}>
            <input name="title" placeholder="Название (необязательно)" />
            <input name="file" type="file" accept=".txt,.md,.pdf,.docx" required />
            <button disabled={busy}>Загрузить и индексировать</button>
          </form>
          <div className="registry">{documents.map((document) =>
            <div key={document.id}>
              <span><b>{document.title}</b><small>{document.filename} · {document.status} · фрагментов: {document.chunks}</small></span>
              <button onClick={() => toggle(document)}>{document.active ? "Деактивировать" : "Восстановить"}</button>
            </div>
          )}</div>
        </section>
      )}
    </main>
  );
}
