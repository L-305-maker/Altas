/** 同源代理：后端地址仅来自部署环境，不接受浏览器指定目标 URL。 */
import { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path } = await context.params;
  const base = process.env.AGENTFLOW_BACKEND_URL || "http://127.0.0.1:8000";
  const target = `${base.replace(/\/$/, "")}/api/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const headers = new Headers({
    Authorization: request.headers.get("Authorization") || "",
  });
  for (const key of ["Content-Type", "Last-Event-ID", "Idempotency-Key"]) {
    const value = request.headers.get(key);
    if (value) headers.set(key, value);
  }
  let body: ArrayBuffer | undefined;
  if (request.method === "POST" && request.body) {
    const reader = request.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 2_000_000) {
        await reader.cancel();
        return Response.json({ detail: "请求超过 2 MB" }, { status: 413 });
      }
      chunks.push(value);
    }
    const combined = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      combined.set(chunk, offset);
      offset += chunk.byteLength;
    }
    body = combined.buffer;
  }
  try {
    const response = await fetch(target, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "manual",
      signal: request.signal,
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type":
          response.headers.get("Content-Type") || "application/json",
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
      },
    });
  } catch {
    return Response.json(
      { detail: "无法连接 AgentFlow API，请检查后端服务。" },
      { status: 502 },
    );
  }
}
export const GET = proxy;
export const POST = proxy;
