import type { NextConfig } from "next";

// next.config is evaluated at build time and `output: "standalone"` bakes the
// rewrite table into the build. The proxy target is therefore fixed when
// `next build` runs: the Dockerfile passes API_INTERNAL_URL as a build ARG
// (default http://api:8000, the Compose service name). Changing the target
// means rebuilding the image. The status line on the home page reads the same
// variable at runtime, so keep the two in agreement.
const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:4601";

// Next's production build emits inline bootstrap scripts and the app uses
// inline styles, so script-src and style-src need 'unsafe-inline'. Everything
// else is locked to same origin; eval is not allowed.
const contentSecurityPolicy = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiInternalUrl}/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Content-Security-Policy", value: contentSecurityPolicy },
        ],
      },
    ];
  },
};

export default nextConfig;
