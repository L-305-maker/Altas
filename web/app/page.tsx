"use client";

import { useCallback, useEffect, useState } from "react";

type Run = { id: string; status: string; created_at: number };
type Step = { id: string; status: string; attempts: number; output: unknown; error: string | null };
type Approval = { id: string; decision: string; actor: string | null };
type Artifact = { id: string; digest: string; content: unknown };
type Detail = Run & { steps: Step[]; approvals: Approval[]; artifacts: Artifact[] };
type Event = { id: number; kind: string; data: Record<string, unknown>; created_at: number };
type Document = { title: string; text: string };
type Draft = { title: string; citations: { source_id: string; title: string; quote: string }[] };
const labels: Record<string, string> = { pending: "待调度", ready: "就绪", running: "执行中", waiting: "等待审批", succeeded: "已完成", failed: "失败", cancelled: "已取消" };

function Status({ value }: { value: string }) {
  return <span className={`status ${value}`}>{labels[value] || value}</span>;
}

export default function Workbench() {
  // 凭据只保存在 React state；刷新页面后需要重新输入。
  const [credential, setCredential] = useState("");
  const [token, setToken] = useState("");
  const [mode, setMode] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [title, setTitle] = useState("");
  const [source, setSource] = useState("");
  const [documents, setDocuments] = useState<Document[]>([]);
  const [actor, setActor] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const api = useCallback(async (path: string, init: RequestInit = {}) => {
    const response = await fetch(`/api${path}`, { ...init, headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...init.headers } });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "请求校验失败，请检查输入。");
    return data;
  }, [token]);

  useEffect(() => {
    if (!token) return;
    let active = true;
    const refresh = async () => {
      try {
        const [list, meta] = await Promise.all([api("/runs"), api("/meta")]);
        if (active) { setRuns(list); setMode(meta.provider); }
        if (selected) {
          const data = await api(`/runs/${selected}`);
          if (active) setDetail(data);
        }
      } catch (e) { if (active) setError((e as Error).message); }
    };
    void refresh();
    const timer = setInterval(refresh, 2000);
    return () => { active = false; clearInterval(timer); };
  }, [token, selected, api]);

  useEffect(() => {
    setEvents([]);
    setDetail(null);
    if (!selected || !token) return;
    const abort = new AbortController();
    // fetch 读取 SSE，凭据使用 Authorization 头，避免 EventSource URL 暴露 token。
    const listen = async () => {
      try {
        const response = await fetch(`/api/runs/${selected}/events`, { headers: { Authorization: `Bearer ${token}` }, signal: abort.signal });
        if (!response.ok || !response.body) throw new Error("事件连接失败");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() || "";
          for (const frame of frames) {
            const line = frame.split("\n").find(line => line.startsWith("data: "));
            if (line) { const event = JSON.parse(line.slice(6)) as Event; setEvents(old => [...old.filter(item => item.id !== event.id), event].slice(-100)); }
          }
        }
      } catch (e) { if (!abort.signal.aborted) setError((e as Error).message); }
    };
    void listen();
    return () => abort.abort();
  }, [token, selected]);

  async function action(work: () => Promise<void>) {
    setBusy(true); setError("");
    try { await work(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  async function create() {
    const docs = documents.length ? documents : [{ title: "手动输入", text: source }];
    const result = await api("/guidelines", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ title, documents: docs }) });
    setSelected(result.id);
    setRuns(await api("/runs"));
  }

  async function decide(id: string, approve: boolean) {
    await api(`/approvals/${id}`, { method: "POST", body: JSON.stringify({ approve, actor, reason }) });
    setDetail(await api(`/runs/${selected}`));
  }

  function download(artifact: Artifact) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(artifact.content, null, 2)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `guideline-${artifact.id}.json`; link.click(); URL.revokeObjectURL(url);
  }

  const draft = detail?.steps.find(step => step.id === "draft")?.output as Draft | undefined;

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><span className="brandmark">A</span><div>AgentFlow<small>证据工作台</small></div></div>
      <div className="navlabel">WORKSPACE</div><div className="navitem">◈　工作流与审批</div>
      <div className="connection"><h3>连接工作台</h3><label htmlFor="token">Operator token</label><input id="token" type="password" autoComplete="off" value={credential} onChange={e => setCredential(e.target.value)} placeholder="输入部署环境配置的 token" />
        <button onClick={() => { setToken(credential); setError(""); }} disabled={!credential}>连接</button>
        {token && <button className="secondary" onClick={() => { setToken(""); setCredential(""); setRuns([]); setSelected(""); setMode(""); }}>断开连接</button>}
        <small>凭据仅在当前页面内存中保存。</small></div>
      <div className="sidebarfoot"><span className="dot" /> {mode ? `${mode === "mock" ? "离线测试模式" : "模型模式"}` : "尚未连接"}<small>AgentFlow / v1.0</small></div>
    </aside>

    <main>
      <header><div><p className="eyebrow">EVIDENCE → REVIEW → REVISION</p><h1>让每一份草稿，都有据可查。</h1><p>导入本地证据，跟踪执行过程，在人工审核后保存独立版本。</p></div><span className="version">LOCAL WORKSPACE</span></header>
      {error && <div className="error" role="alert">{error}<button aria-label="关闭错误提示" onClick={() => setError("")}>×</button></div>}
      {mode === "mock" && <div className="notice">当前使用离线 mock：用于验证流程与来源追溯，不代表模型推理质量。</div>}

      <section className="topgrid">
        <article className="panel"><div className="panelhead"><h2>创建证据草稿</h2><span className="stepnumber">01</span></div>
          <form onSubmit={e => { e.preventDefault(); void action(create); }}>
            <label htmlFor="title">草稿标题</label><input id="title" value={title} onChange={e => setTitle(e.target.value)} required maxLength={200} placeholder="例如：某主题证据整理 · 第一次修订" />
            <label htmlFor="files" className="upload">＋ 导入 .txt / .md 证据文档<input id="files" type="file" accept=".txt,.md" multiple onChange={e => { const files = Array.from(e.target.files || []); void action(async () => { if (files.length > 20 || files.reduce((sum, file) => sum + file.size, 0) > 1_500_000) throw new Error("最多 20 份文件，总大小不超过 1.5 MB"); setDocuments(await Promise.all(files.map(async file => ({ title: file.name, text: await file.text() })))); }); }} /></label>
            {documents.length > 0 ? <div className="filelist">{documents.map((doc, i) => <span key={i}>{doc.title}</span>)}<button type="button" className="textbutton" onClick={() => setDocuments([])}>清空文件</button></div> : <><label htmlFor="source">或粘贴证据原文</label><textarea id="source" value={source} onChange={e => setSource(e.target.value)} rows={5} required maxLength={200000} placeholder="原文会保留来源摘要，生成的引文必须能在原文中找到。" /></>}
            <button type="submit" disabled={!token || busy || !title || (!source && !documents.length)}>创建并开始执行 <span>↗</span></button>
          </form>
        </article>
        <article className="panel runs"><div className="panelhead"><h2>运行记录</h2><span className="count">{runs.length}</span></div>
          {!runs.length ? <div className="empty">暂无运行记录。<small>连接工作台后，创建第一份证据草稿。</small></div> : runs.map(run => <button key={run.id} className={`runrow ${selected === run.id ? "selected" : ""}`} onClick={() => setSelected(run.id)}><div><strong>{run.id.slice(0, 8)}</strong><small>{new Date(run.created_at * 1000).toLocaleString("zh-CN")}</small></div><Status value={run.status} /></button>)}
        </article>
      </section>

      <section className="panel execution"><div className="panelhead"><div><p className="eyebrow">EXECUTION DETAIL</p><h2>{selected ? `运行 ${selected.slice(0, 8)}` : "查看一次运行"}</h2></div>{detail && <div className="actions"><Status value={detail.status} />{!["succeeded", "failed", "cancelled"].includes(detail.status) && <button className="secondary" disabled={busy} onClick={() => void action(async () => { await api(`/runs/${selected}/cancel`, { method: "POST" }); setDetail(await api(`/runs/${selected}`)); })}>取消运行</button>}</div>}</div>
        {!detail ? <div className="empty">选择一条运行记录，查看步骤、审批与版本。</div> : <>
          <div className="steps">{detail.steps.map(step => <div className="step" key={step.id}><div><strong>{step.id}</strong><Status value={step.status} /></div><small>执行次数 {step.attempts}{step.error ? ` · ${step.error}` : ""}</small></div>)}</div>
          <div className="detailgrid"><div><h3>草稿预览</h3>{draft && Array.isArray(draft.citations) && <div className="draft"><h2>{draft.title}</h2>{draft.citations.map((citation, index) => <blockquote key={index}><p>{citation.quote}</p><cite>{citation.title} · 来源 {citation.source_id.slice(0, 12)}</cite></blockquote>)}</div>}
            <details><summary>查看步骤检查点</summary>{detail.steps.filter(step => step.output).map(step => <div key={step.id}><h3>{step.id}</h3><pre>{JSON.stringify(step.output, null, 2)}</pre></div>)}</details>
            {detail.approvals.filter(a => a.decision === "pending").map(approval => <div className="approval" key={approval.id}><h3>需要你的审核</h3><p>核对上方引文和来源后，批准保存版本或拒绝本次草稿。</p><label htmlFor="actor">审核人</label><input id="actor" value={actor} maxLength={80} onChange={e => setActor(e.target.value)} placeholder="填写审核人标识" /><label htmlFor="reason">审核说明</label><textarea id="reason" value={reason} maxLength={1000} onChange={e => setReason(e.target.value)} rows={2} /><div className="actions"><button disabled={busy || !actor.trim()} onClick={() => void action(() => decide(approval.id, true))}>批准并保存版本</button><button className="secondary" disabled={busy || !actor.trim()} onClick={() => void action(() => decide(approval.id, false))}>拒绝</button></div></div>)}
            {detail.artifacts.map(artifact => <div className="artifact" key={artifact.id}><div><h3>已保存版本</h3><small>SHA-256: {artifact.digest.slice(0, 20)}…</small></div><button className="secondary" onClick={() => download(artifact)}>下载 JSON ↓</button></div>)}
          </div><div className="events"><h3>事件时间线 <span className="live">LIVE</span></h3>{events.length ? events.map(event => <div className="event" key={event.id}><i /><div><strong>{event.kind}</strong><small>{new Date(event.created_at * 1000).toLocaleTimeString("zh-CN")} {event.data.step ? ` · ${event.data.step}` : ""}</small></div></div>) : <p className="muted">等待事件…</p>}</div></div>
        </>}
      </section>
      <footer>持久执行 · 明确审批 · 可追溯来源<span>每一次执行，都是独立的版本。</span></footer>
    </main>
  </div>;
}
