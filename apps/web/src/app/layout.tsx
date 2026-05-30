import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/Sidebar";

export const metadata: Metadata = {
  title: "ASIL — Engineering Intelligence",
  description:
    "Persistent, temporal, causal understanding of how a software system evolves, behaves, and fails.",
};

const STATIC_MODE = process.env.NEXT_PUBLIC_STATIC_MODE === "1";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="font-sans">
        <div className="flex h-screen w-screen overflow-hidden">
          <Sidebar />
          <main className="flex-1 overflow-y-auto scroll-pane bg-gradient-to-br from-ink-900 via-ink-900 to-ink-800">
            {STATIC_MODE && (
              <div className="bg-warn/15 border-b border-warn/40 text-warn text-xs px-6 py-2">
                <span className="font-semibold">Demo snapshot.</span> This
                deployment runs against frozen JSON fixtures so it can live on
                GitHub Pages. For live data, run ASIL locally:{" "}
                <code className="font-mono text-ink-100">make up</code> +{" "}
                <code className="font-mono text-ink-100">
                  uv run uvicorn asil_api.main:app
                </code>{" "}
                + <code className="font-mono text-ink-100">pnpm dev</code>.
              </div>
            )}
            <div className="mx-auto max-w-6xl p-6 lg:p-10">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
