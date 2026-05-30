/** @type {import('next').NextConfig} */

// Two build modes:
//   - `pnpm dev` / `pnpm build` (default): live app against FastAPI.
//   - `NEXT_STATIC=1 pnpm build`: static export for GitHub Pages.
//     Reads JSON fixtures bundled under public/snapshot/ instead of
//     hitting an API. Used by .github/workflows/gh-pages.yml.

const isStatic = process.env.NEXT_STATIC === "1";
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

const nextConfig = {
  reactStrictMode: true,
  ...(isStatic
    ? {
        output: "export",
        // GitHub Pages serves from a project subpath (`/ASIL/`).
        // basePath is propagated to NEXT_PUBLIC_BASE_PATH so client
        // fixture URLs build correctly.
        basePath,
        trailingSlash: true,
        // GitHub Pages can't run the next/image optimizer.
        images: { unoptimized: true },
      }
    : {}),
  env: {
    NEXT_PUBLIC_ASIL_API_URL:
      process.env.NEXT_PUBLIC_ASIL_API_URL ?? "http://localhost:8000",
    NEXT_PUBLIC_STATIC_MODE: isStatic ? "1" : "0",
    NEXT_PUBLIC_BASE_PATH: basePath,
  },
};

export default nextConfig;
