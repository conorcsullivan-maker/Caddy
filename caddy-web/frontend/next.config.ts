import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    // In local dev, proxy /api/* to the local FastAPI backend.
    // In production, vercel.json handles the equivalent rewrite to Render.
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8000/api/:path*",
      },
    ];
  },
};

export default nextConfig;
