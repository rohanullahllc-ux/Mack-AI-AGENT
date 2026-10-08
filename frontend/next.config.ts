import type { NextConfig } from "next";

// The dashboard talks to the FastAPI backend through this same-origin proxy, so the browser/phone
// only ever needs one URL (and a single Cloudflare Tunnel can expose the whole app).
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // Compression buffers chunks and would stall the live SSE agent stream.
  compress: false,
  // Hide the dev-only "N" badge; it sits on top of the sidebar budget card.
  devIndicators: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` },
      { source: "/health", destination: `${BACKEND_URL}/health` },
    ];
  },
};

export default nextConfig;
