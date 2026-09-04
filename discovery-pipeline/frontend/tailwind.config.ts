import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        discovery: {
          bg:      "#0a0a0f",
          surface: "#12121a",
          border:  "#1e1e2e",
          accent:  "#7c3aed",
          hot:     "#ef4444",
          warm:    "#f59e0b",
          cool:    "#6b7280",
        },
      },
    },
  },
  plugins: [],
};
export default config;
