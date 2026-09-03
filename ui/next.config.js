/** @type {import('next').NextConfig} */
// Static export: served by FastAPI from ui/out (mounted last at "/").
// Rewrites are unavailable under `output: "export"`; `next dev` talks to the
// backend through NEXT_PUBLIC_API (e.g. http://127.0.0.1:8000) instead.
const nextConfig = {
  output: "export",
  images: { unoptimized: true },
  trailingSlash: true,
  reactStrictMode: true,
};
module.exports = nextConfig;
