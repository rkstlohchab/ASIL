import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          50: "#f7f7f8",
          100: "#eceef1",
          200: "#d4d8df",
          300: "#a9b1bd",
          400: "#7c8595",
          500: "#525c6f",
          600: "#3a4254",
          700: "#272e3d",
          800: "#181e2a",
          900: "#0c1019",
        },
        accent: {
          400: "#8fb9ff",
          500: "#5b8cff",
          600: "#3568e0",
        },
        ok: "#3ed598",
        warn: "#ffb950",
        danger: "#ff6b6b",
      },
      fontFamily: {
        sans: [
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "Helvetica",
          "Arial",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas"],
      },
    },
  },
  plugins: [],
};

export default config;
