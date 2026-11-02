// ESLint for the scripts in newsreader/static (npm run lint). Prettier owns the layout (npm run format); these rules
// catch mistakes. The rules block is the same as Infra Game's.
import js from "@eslint/js";
import globals from "globals";

export default [
  // Saved news pages (and their scripts) and this PC's data are not ours to lint.
  { ignores: ["articles/", "instance/"] },
  js.configs.recommended,
  {
    rules: {
      // An unused catch binding, or an argument kept for a shared signature such as (argv, ctx), is fine.
      "no-unused-vars": ["warn", { args: "none", caughtErrors: "none" }],
      "no-useless-assignment": "warn",
      "no-useless-escape": "warn",
      "preserve-caught-error": "warn",
      // Japanese text has full-width spaces.
      "no-irregular-whitespace": ["error", { skipStrings: true, skipTemplates: true, skipRegExps: true }],
      // el.textContent = el.textContent and the like go through a setter on purpose.
      "no-self-assign": ["error", { props: false }],
    },
  },
  {
    files: ["newsreader/static/**/*.js"],
    languageOptions: {
      sourceType: "script",
      // NR: the namespace the scripts share (window.NR). module: numcheck.js hands itself to Node for the tests.
      globals: { ...globals.browser, NR: "readonly", module: "readonly" },
    },
  },
];
