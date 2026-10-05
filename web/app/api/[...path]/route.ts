/** 同源代理：后端地址仅来自部署环境，不接受浏览器指定目标 URL。 */
import { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  const base = process.env.AGENTFLOW_BACKEND_URL || "http://127.0.0.1:8000";
  const target = `${base.replace(/\/$/, "")}/api/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  const headers = new Headers({ "Authorization": request.headers.get("Authorization") || "" });
  for (const key of ["Content-Type", "Last-Event-ID", "Idempotency-Key"]) {
    const value = request.headers.get(key);
    if (value) headers.set(key, value);
  }
  const body = request.method === "POST" ? await request.arrayBuffer() : undefined;
  if (body && body.byteLength > 2_000_000) return Response.json({ detail: "请求超过 2 MB" }, { status: 413 });
  try {
    const response = await fetch(target, { method: request.method, headers, body, cache: "no-store", redirect: "manual", signal: request.signal });
    return new Response(response.body, { status: response.status, headers: {
      "Content-Type": response.headers.get("Content-Type") || "application/json",
      "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    } });
  } catch {
    return Response.json({ detail: "无法连接 AgentFlow API，请检查后端服务。" }, { status: 502 });
  }
}
export const GET = proxy;
export const POST = proxy;
