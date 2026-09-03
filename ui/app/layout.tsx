import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Deltr - CEX / DEX delta-neutral arbitrage agent",
  description: "Long PancakeSwap V3 on BNB Chain, short the Binance USDS-M perp, delta-neutral by construction. Built on Binance Agent OS + MCP.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-ink-950 text-gray-200 antialiased">{children}</body>
    </html>
  );
}
