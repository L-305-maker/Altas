import type { Metadata } from "next";
import "./globals.css";
import "./preview.css";

export const metadata: Metadata = { title: "AgentFlow · 证据工作台", description: "可追溯、可审批的持久工作流" };

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
