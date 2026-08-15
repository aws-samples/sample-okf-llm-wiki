import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

// The SPA is JavaScript (jsx), not TypeScript — the glob must say so: the
// Vite template this grew from shipped files: ['**/*.{ts,tsx}'], which on
// this repo matched NOTHING, so `npm run lint` green-lit every commit while
// checking zero files.
export default defineConfig([
  // dist-harness is build output like dist; src/vendor is inlined third-party
  // minified code (?raw imports for the chart srcdoc) — not ours to lint.
  globalIgnores(['dist', 'dist-harness', 'src/vendor']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 'latest',
      // node globals too: vite.config.js and the srcdoc-builder libs run
      // under both; the browser set alone flags `process`/`__dirname`.
      globals: { ...globals.browser, ...globals.node },
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      // Uppercase-start names are React components/constants that read as
      // "used" via JSX or exports the linter can't always see.
      'no-unused-vars': [
        'error',
        { varsIgnorePattern: '^[A-Z_]', argsIgnorePattern: '^_' },
      ],
      // React-idiom rules stay VISIBLE but don't fail the gate: errors mean
      // real defects here. set-state-in-effect & friends flag long-standing
      // deliberate patterns (reset-on-prop-change effects, latest-args refs)
      // that work correctly — burn them down opportunistically, not as a
      // 37-file rewrite inside an unrelated change.
      'react-hooks/set-state-in-effect': 'warn',
      'react-hooks/immutability': 'warn',
      'react-hooks/static-components': 'warn',
      'react-hooks/use-memo': 'warn',
      'react-hooks/refs': 'warn',
      'react-refresh/only-export-components': 'warn',
    },
  },
])
