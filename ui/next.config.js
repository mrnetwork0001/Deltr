/** @type {import('next').NextConfig} */
// Static export: served by FastAPI from ui/out (mounted last at "/").
// Rewrites are unavailable under `output: "export"`; `next dev` talks to the
// backend through NEXT_PUBLIC_API (e.g. http://127.0.0.1:8000) instead.
const nextConfig = {
  // On Vercel the static site fronts two engines through vercel.json rewrites: the PAPER
  // showcase at "/" and the LIVE one under "/live". Elsewhere (FastAPI serving ui/out, next
  // dev) there is one engine and no switcher, unless NEXT_PUBLIC_ENGINES says otherwise.
  env: {
    NEXT_PUBLIC_ENGINES: process.env.NEXT_PUBLIC_ENGINES ?? (process.env.VERCEL ? "paper=|live=/live" : ""),
  },
  output: "export",
  images: { unoptimized: true },
  trailingSlash: true,
  reactStrictMode: true,
};
module.exports = nextConfig;
