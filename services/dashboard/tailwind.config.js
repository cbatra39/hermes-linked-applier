/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Neutral "ink" ramp - the dashboard chrome. Dark by default.
        ink: {
          950: '#06080b',
          900: '#0a0d13',
          875: '#0d1118',
          850: '#11161f',
          800: '#151b26',
          750: '#1a212d',
          700: '#212a38',
          600: '#2c3646',
          500: '#3d4a5c',
          400: '#5a6980',
          300: '#8593a8',
          200: '#b4bfcf',
          100: '#dde3ec',
        },
        // Brand accent (Hermes) - cyan/teal, WCAG-AA on ink-900 backgrounds.
        brand: {
          50: '#e6fbff',
          200: '#9ceaf7',
          300: '#67dcf0',
          400: '#2ec8e4',
          500: '#12aac9',
          600: '#0d87a3',
          700: '#0b6b7f',
          800: '#0a5464',
        },
        good: { 400: '#4ade80', 500: '#22c55e', 600: '#16a34a' },
        warn: { 400: '#fbbf24', 500: '#f59e0b', 600: '#d97706' },
        bad: { 400: '#f87171', 500: '#ef4444', 600: '#dc2626' },
        info: { 400: '#818cf8', 500: '#6366f1' },
      },
      fontFamily: {
        // Local system stacks only - no external font downloads (offline / self-hosted).
        sans: [
          'ui-sans-serif',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'sans-serif',
        ],
        mono: [
          'ui-monospace',
          'SFMono-Regular',
          'Menlo',
          'Consolas',
          'Liberation Mono',
          'Courier New',
          'monospace',
        ],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      boxShadow: {
        panel: '0 1px 0 0 rgba(255,255,255,0.03) inset, 0 8px 24px -12px rgba(0,0,0,0.7)',
        drawer: '0 -12px 40px -12px rgba(0,0,0,0.85)',
      },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
        'slide-up': {
          from: { transform: 'translateY(12px)', opacity: '0' },
          to: { transform: 'translateY(0)', opacity: '1' },
        },
        pulseline: { '0%,100%': { opacity: '0.35' }, '50%': { opacity: '1' } },
      },
      animation: {
        'fade-in': 'fade-in 140ms ease-out',
        'slide-up': 'slide-up 160ms ease-out',
        pulseline: 'pulseline 1.4s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
