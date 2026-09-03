import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: {
    relative: true,
    files: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  },
  theme: {
    extend: {
      colors: {
        ink: { 950: "#07090f", 900: "#0b0f19", 800: "#111827", 700: "#1f2937" },
        bnb: { DEFAULT: "#F0B90B", dim: "#c99a05" },
        cake: { DEFAULT: "#1FC7D4" },
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
export default config;
