import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/Sidebar";

export const metadata: Metadata = {
  title: "ASIL — Engineering Intelligence",
  description:
    "Persistent, temporal, causal understanding of how a software system evolves, behaves, and fails.",
};

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
            <div className="mx-auto max-w-6xl p-6 lg:p-10">{children}</div>
          </main>
        </div>
      </body>
    </html>
  );
}
